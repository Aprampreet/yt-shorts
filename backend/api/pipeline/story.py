"""Story Studio pipeline stages, run as GenerationJob rows with pipeline='story' and a ``step``.

Steps: analyze (source intelligence), write (op=concepts | scripts | regenerate), render (story videos). Each task
claims its job atomically (queued -> active), checks for cancellation between stages via ``update`` and saves
per-stage timings in ``job.analysis['timings']``.
"""
from __future__ import annotations

import logging
import shutil
import time

from billiard.exceptions import SoftTimeLimitExceeded
from django.db import transaction
from django.utils import timezone

from ..editing import story_compiler
from ..editing.render import render_timeline
from ..models import GeneratedScript, GeneratedShort, GenerationJob, GenerationProject, MediaAsset, StoryConcept, \
    TranscriptSegment
from ..providers import Capability, ProviderError, ProviderRateLimited, registry
from ..services import assets as asset_svc
from ..services.groq import finalize_description
from ..services.media import StudioError, run
from ..story import intelligence as intel
from ..story import scoring, scripts as writer
from ..story.scripts import TextProviderUnavailable
from . import ingest, transcribe
from .context import Cancelled, JobContext, relative, update
from .states import STEP_STATES, TERMINAL

log = logging.getLogger(__name__)
FORMAT_TAGS = {'gaming_news': '#GamingNews', 'gameplay_analysis': '#GameplayAnalysis', 'hidden_details': '#HiddenDetails',
               'game_comparison': '#GameComparison', 'explainer': '#GamingExplained', 'gaming_commentary': '#GamingCommentary',
               'myth_vs_fact': '#MythVsFact', 'top_discoveries': '#TopDiscoveries'}
SYNTHETIC_DISCLOSURE = 'This video is narrated with a synthetic (AI-generated) voice.'
FACT_NOTE = ('Original commentary. Statements attributed to the source are reported as stated there and were not '
             'independently fact-checked.')
COPYRIGHT_NOTE = ('The script is original writing based on source facts; AI rewriting is not copyright clearance. '
                  'Third-party footage, music and images keep their own licences.')
LANG_CODES = {'en': 'en', 'hi': 'hi', 'hinglish': 'hi-Latn'}


# ------------------------------------------------------------------------------------- job runtime

def claim(job_id):
    step_state = None
    job = GenerationJob.objects.filter(pk=job_id, pipeline='story').first()
    if job is None:
        return None
    step_state = STEP_STATES.get(job.step, 'analyzing')
    if not GenerationJob.objects.filter(pk=job_id, status='queued').update(
            status=step_state, started_at=timezone.now(), error_code='', error_message=''):
        return None
    return GenerationJob.objects.select_related('project', 'source').get(pk=job_id)


def set_project_status(project, status):
    GenerationProject.objects.filter(pk=project.pk).update(status=status, updated_at=timezone.now())


def fail(job, code, message, extra=None):
    analysis = dict(job.analysis or {}, **(extra or {}))
    GenerationJob.objects.filter(pk=job.pk).exclude(status='cancelled').update(
        status='failed', stage='Action needed', error_code=code, error_message=message, analysis=analysis,
        completed_at=timezone.now())


def finish(ctx, status, stage, **analysis):
    job = ctx.job
    job.analysis = dict(job.analysis or {}, timings=ctx.timings, warnings=ctx.warnings,
                        total_seconds=round(time.monotonic() - ctx.started, 2), **analysis)
    update(job, status, stage, 100)
    GenerationJob.objects.filter(pk=job.pk).update(analysis=job.analysis, completed_at=timezone.now())


def run_job(job_id):
    """Celery entry point shared by every story task. Duplicate deliveries are no-ops (atomic claim)."""
    job = claim(job_id)
    if job is None:
        return
    project = job.project
    ctx = JobContext.create(job)
    set_project_status(project, 'active')
    try:
        STEPS[job.step](ctx, project)
    except Cancelled:
        log.info('Story job cancelled: %s', job.pk)
    except TextProviderUnavailable as exc:
        fail(job, exc.code, exc.message, {'provider_reasons': exc.reasons, 'timings': ctx.timings})
    except ProviderRateLimited as exc:
        fail(job, exc.code, exc.message, {'retry_after': exc.retry_after.isoformat() if exc.retry_after else None,
                                          'timings': ctx.timings})
    except (StudioError, SoftTimeLimitExceeded) as exc:
        fail(job, getattr(exc, 'code', 'job_timeout'),
             getattr(exc, 'message', 'Processing exceeded its time limit.'), {'timings': ctx.timings})
    except Exception:
        log.exception('Story job failed: %s', job.pk)
        fail(job, 'internal_error', 'Processing failed. Check worker logs and retry.', {'timings': ctx.timings})
    finally:
        shutil.rmtree(ctx.folders['audio'], ignore_errors=True)
        for partial in ctx.folders['renders'].glob('*.partial.mp4'):
            partial.unlink(missing_ok=True)
        job.refresh_from_db(fields=['status'])
        if job.status == 'failed':
            set_project_status(project, 'failed' if job.step == 'render' else 'needs_review' if project.scripts.exists() else 'draft')
        elif job.status in TERMINAL and job.status != 'cancelled':
            set_project_status(project, 'completed' if job.status == 'completed' and job.step == 'render'
                               else 'needs_review' if job.status == 'needs_review' else 'active')


# ------------------------------------------------------------------------------------- analyze

def register_source_asset(ctx, project):
    """Keep the authorised source as a library asset so it can back source_clip scenes (third-party footage)."""
    source = project.source
    path = ctx.source
    if path is None or not path.exists():
        return None
    folder = asset_svc.owner_dir(project.owner)/'sources'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/f'{source.pk}{path.suffix.lower() or ".mp4"}'
    shutil.move(str(path), target)
    ctx.source = target
    asset = asset_svc.register_generated_asset(project.owner, target, 'video', 'source_ingest', 'permission_pending',
                                               title=(source.title or 'Source video')[:300],
                                               metadata={'third_party_source_footage': True})
    MediaAsset.objects.filter(pk=asset.pk).update(
        source_video=source, source_url=(source.source_url or '')[:200],
        rights_notes='Third-party source footage. The user declared rights/permission; not independently verified.')
    return asset


def carry_evidence(old, new):
    verified = {intel.flat(c['text']): c for c in (old or {}).get('claims') or [] if c.get('status') == 'verified'}
    for claim in new.get('claims') or []:
        prior = verified.get(intel.flat(claim['text']))
        if prior:
            claim.update(status='verified', evidence=prior.get('evidence'), previous_status=claim['status'])


def analyze(ctx, project):
    job = ctx.job
    cues, transcript_info = [], {}
    if project.source_id and project.source.source_url:
        tick = time.monotonic()
        ctx.attach_providers()
        ingest.acquire_source(ctx)
        ingest.inspect_source(ctx)
        transcribe.transcribe(ctx)
        cues = ctx.cues
        transcript_info = {'cues': len(cues), 'source_duration': ctx.info.get('duration'),
                           'note': 'Transcribed from the authorised source audio; no YouTube captions were fetched.'}
        update(job, 'analyzing', 'Saving the authorised source for optional source clips', 60)
        try:
            register_source_asset(ctx, project)
        except StudioError as exc:
            ctx.warnings.append(f'Source footage was not kept for source clips: {exc.message}')
        ctx.mark('source', tick)
        if project.source.title and not project.title:
            GenerationProject.objects.filter(pk=project.pk).update(title=project.source.title[:300])
    update(job, 'analyzing', 'Extracting sections and claims', 70)
    tick = time.monotonic()
    adapter, error = None, None
    try:
        adapter = writer.text_provider(job)
    except TextProviderUnavailable as exc:
        error = exc
    try:
        result = intel.build_intelligence(project.source_notes, cues, adapter, error)
    except ProviderError as exc:
        result = intel.build_intelligence(project.source_notes, cues, None, exc)
        result['notes'].insert(0, f'The text provider failed ({exc.message}); claims were extracted deterministically.')
        result['text_provider_unavailable'] = True
    result['transcript'] = transcript_info
    carry_evidence(project.intelligence, result)
    project.refresh_from_db(fields=['intelligence', 'title'])
    old_selection = (project.intelligence or {}).get('concept_selection')
    if old_selection:
        result['concept_selection'] = old_selection
    project.intelligence = result
    project.save(update_fields=['intelligence', 'updated_at'])
    ctx.mark('intelligence', tick)
    finish(ctx, 'completed', 'Source analysed', method=result['method'], claims=len(result['claims']),
           sections=len(result['sections']), text_provider_unavailable=result['text_provider_unavailable'])


# ------------------------------------------------------------------------------------- write

def write_concepts(ctx, project):
    job = ctx.job
    adapter = writer.text_provider(job)
    update(job, 'writing', 'Generating six candidate concepts', 20)
    tick = time.monotonic()
    n = int((project.inputs or {}).get('video_count', 3))
    candidates, picked, report = writer.generate_concepts(project, n, job=job, adapter=adapter)
    with transaction.atomic():
        project.concepts.filter(scripts__isnull=True).delete()
        project.concepts.update(selected=False)
        base = max(project.concepts.values_list('index', flat=True), default=0)
        revision = max(project.concepts.values_list('revision', flat=True), default=0) + 1
        rows = []
        for i, c in enumerate(candidates):
            rows.append(StoryConcept(project=project, index=base + i + 1, angle=c['angle'], title=c['title'],
                                     logline=c['logline'], hook_idea=c['hook_idea'], key_points=c['key_points'],
                                     claim_ids=c['claim_ids'], diversity_score=c['diversity_score'],
                                     quality_score=c['quality_score'], selected=i in picked, revision=revision,
                                     provider=report['provider']))
        StoryConcept.objects.bulk_create(rows)
        project.refresh_from_db(fields=['intelligence'])
        project.intelligence = dict(project.intelligence or {}, concept_selection=report)
        project.save(update_fields=['intelligence', 'updated_at'])
    ctx.mark('concepts', tick)
    finish(ctx, 'completed', 'Concepts ready', concepts=len(rows), selected=len(picked), diversity=report)


def new_version(concept):
    return (max(concept.scripts.values_list('version', flat=True), default=0) + 1) if concept else 1


def write_scripts(ctx, project):
    job = ctx.job
    adapter = writer.text_provider(job)
    inputs = project.inputs or {}
    n = int(inputs.get('video_count', 3))
    concepts = list(project.concepts.filter(selected=True).order_by('index')[:n])
    if not concepts:
        raise StudioError('no_concepts_selected', 'Select at least one concept before writing scripts.')
    force = bool(job.settings.get('force'))
    written, review = [], 0
    lo, hi = scoring.target_window(inputs.get('target_duration', '30_45'))
    for i, concept in enumerate(concepts, 1):
        if concept.scripts.exists() and not force:
            continue
        update(job, 'writing', f'Writing script {i} of {len(concepts)}', 10 + int(80 * (i - 1) / len(concepts)))
        tick = time.monotonic()
        out, report, used, history = writer.write_script(project, concept, job=job, adapter=adapter)
        script = GeneratedScript(project=project, concept=concept, version=new_version(concept),
                                 language=project.language, narration_style=inputs.get('narration_style', ''),
                                 target_duration=hi, attempts=used, provider=writer.provider_name(adapter))
        sections = dict(out.sections.model_dump(), quotes=list(out.quotes))
        writer.apply_report(script, sections, dict(report, history=history), title=out.title)
        script.save()
        written.append(str(script.pk))
        review += script.status == 'needs_review'
        ctx.mark(f'script_{i}', tick)
    finish(ctx, 'needs_review' if review else 'completed',
           f'{len(written)} script(s) written' + (f'; {review} need review' if review else ''),
           scripts=written, needs_review=review)


def regenerate(ctx, project):
    job = ctx.job
    script = GeneratedScript.objects.select_related('concept').get(pk=job.settings['script_id'], project=project)
    if script.concept is None:
        raise StudioError('manual_script', 'Manual scripts are not regenerated by AI; edit them directly.')
    left = writer.MAX_TOTAL_ATTEMPTS - script.attempts
    if left <= 0:
        raise StudioError('regeneration_limit', 'This script has used all of its generation attempts.')
    adapter = writer.text_provider(job)
    update(job, 'writing', 'Rewriting the script with targeted feedback', 20)
    tick = time.monotonic()
    out, report, used, history = writer.write_script(project, script.concept, job=job, adapter=adapter,
                                                     attempts=min(writer.MAX_ATTEMPTS_PER_ROUND, left),
                                                     feedback=(script.quality_report or {}).get('reasons'))
    current = script.quality_report or {}
    better = (report['passed'], report['score']) > (bool(current.get('passed')), float(current.get('score') or 0))
    script.attempts += used
    if better:
        sections = dict(out.sections.model_dump(), quotes=list(out.quotes))
        writer.apply_report(script, sections, dict(report, history=history), title=out.title)
        script.narration_asset = None
        script.scenes.all().delete()
    else:
        script.quality_report = dict(current, last_regeneration={'kept': 'previous', 'new_score': report['score'],
                                                                 'history': history})
    script.provider = writer.provider_name(adapter)
    script.save()
    ctx.mark('regenerate', tick)
    finish(ctx, 'completed' if script.status != 'needs_review' else 'needs_review',
           'Script regenerated' if better else 'Kept the better previous draft', script_id=str(script.pk),
           replaced=better, attempts_left=writer.MAX_TOTAL_ATTEMPTS - script.attempts)


def write(ctx, project):
    op = ctx.job.settings.get('op')
    return {'concepts': write_concepts, 'scripts': write_scripts, 'regenerate': regenerate}[op](ctx, project)


# ------------------------------------------------------------------------------------- render

def preflight(project, scripts):
    """Checks shared by the render route and task. Raises StudioError with a clear code."""
    if not scripts:
        raise StudioError('no_scripts', 'Approve at least one script with narration and a storyboard before rendering.')
    for script in scripts:
        label = (script.sections or {}).get('title') or f'script {str(script.pk)[:8]}'
        if script.status != 'approved':
            raise StudioError('script_not_approved', f'"{label}" is not approved yet.')
        if script.narration_asset_id is None or script.narration_asset.deleted_at is not None:
            raise StudioError('narration_required', f'"{label}" has no narration.')
        scenes = list(script.scenes.select_related('asset'))
        if not scenes:
            raise StudioError('storyboard_required', f'"{label}" has no storyboard.')
        for scene in scenes:
            if scene.asset is not None and scene.asset.license_status == 'restricted':
                raise StudioError('restricted_asset', f'"{label}" scene {scene.order} uses the restricted asset '
                                  f'"{scene.asset.title}". Replace it before rendering.')
            if scene.asset is not None and (scene.asset.deleted_at is not None or scene.asset.owner != project.owner):
                raise StudioError('asset_unavailable', f'"{label}" scene {scene.order} uses an asset that is no longer available.')
        if script.narration_asset.license_status == 'restricted':
            raise StudioError('restricted_asset', f'The narration of "{label}" is restricted (for example a TTS plan '
                              'without commercial rights). Use another voice or upload your own narration.')
    music = music_asset(project)
    if music is not None and music.license_status == 'restricted':
        raise StudioError('restricted_asset', f'The background music "{music.title}" is restricted.')
    duplicates = scoring.near_duplicates([s.full_text for s in scripts])
    if duplicates:
        i, j, value = duplicates[0]
        raise StudioError('duplicate_scripts', f'Scripts {i + 1} and {j + 1} have near-identical narration (token overlap '
                          f'{value:.2f} > {scoring.DUPLICATE_JACCARD}). Edit one of them or render only one.')


def music_asset(project):
    music_id = (project.inputs or {}).get('music_asset_id')
    if not music_id:
        return None
    return MediaAsset.objects.filter(pk=music_id, owner=project.owner, deleted_at__isnull=True).first()


def story_metadata(job, project, script):
    sections = script.sections or {}
    title = (sections.get('title') or (script.concept.title if script.concept else '') or project.title or 'Story')[:100]
    hook = scoring.strip_markers(sections.get('hook', ''))
    payoff = scoring.strip_markers(sections.get('payoff', ''))
    fmt = (project.inputs or {}).get('content_format', 'gaming_commentary')
    hashtags = ['#Shorts', '#Gaming', FORMAT_TAGS.get(fmt, '#Gaming')]
    base = ' '.join(x for x in (hook, payoff) if x)[:400]
    source = 'extractive'
    if registry.usable('groq', Capability.TEXT, job=job):
        meta = registry.get('groq', job=job).metadata(script.full_text)
        if meta:
            title, base, hashtags, source = meta['title'][:100], meta['description'], meta['hashtags'], 'groq'
    return title, base, list(dict.fromkeys(hashtags)), source


def write_srt(groups, path):
    def stamp(t):
        ms = int(round(t * 1000))
        return f'{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}'
    lines = []
    for i, (s, e, text) in enumerate(groups, 1):
        lines += [str(i), f'{stamp(s)} --> {stamp(e)}', text, '']
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path


def make_video(ctx, project, script, number, total):
    job = ctx.job
    inputs = project.inputs or {}
    canvas = (1080, 1920) if inputs.get('resolution') == '1080' else (720, 1280)
    workdir = ctx.root/'work'/str(number)
    scenes = list(script.scenes.select_related('asset').order_by('order'))
    update(job, 'rendering', f'Compiling video {number} of {total}', 10 + int(80 * (number - 1) / total))
    tick = time.monotonic()
    compiled = story_compiler.compile_script(script, scenes, workdir, canvas=canvas,
                                             theme=inputs.get('theme', 'gaming_hype'),
                                             caption_preset=inputs.get('caption_preset', 'clean_commentary'),
                                             music_asset=music_asset(project))
    narration = script.narration_asset
    synthetic = narration.origin == 'generated_tts'
    rights = story_compiler.rights_of(compiled.provenance, synthetic_voice=synthetic)
    if rights['blocked']:
        raise StudioError('restricted_asset', 'A restricted asset is used; the render was refused.')
    update(job, 'rendering', f'Rendering video {number} of {total}', 15 + int(80 * (number - 1) / total))
    output = ctx.folders['renders']/f'{number}.mp4'
    result = render_timeline(compiled.timeline, output, workdir)
    update(job, 'quality_checking', f'Checking video {number} of {total}', 20 + int(80 * (number - 1) / total))
    duration = compiled.timeline.duration
    thumbnail = ctx.folders['thumbnails']/f'{number}.jpg'
    run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-ss', f'{min(1.2, duration / 2):.2f}', '-i', output,
         '-frames:v', '1', '-vf', 'scale=270:480', thumbnail])
    srt = write_srt(compiled.groups, ctx.folders['captions']/f'{number}.srt')
    title, base, hashtags, metadata_source = story_metadata(job, project, script)
    extra = []
    if synthetic:
        extra.append(SYNTHETIC_DISCLOSURE)
    if rights['attribution']:
        extra.append('Credits: ' + '; '.join(dict.fromkeys(rights['attribution'])))
    extra.append(FACT_NOTE)
    description = (finalize_description(base, hashtags) + '\n\n' + '\n'.join(extra)).strip()[:5000]
    qa_report = result.report
    review = rights['status'] == 'manual_review_required'
    qa_status = 'flagged' if review and qa_report.get('qa_status') == 'passed' else qa_report.get('qa_status', 'passed')
    report = {
        'kind': 'story', 'qa': qa_report, 'script_quality': script.quality_report,
        'script_quality_basis': scoring.BASIS,
        'narration': {'asset_id': str(narration.pk), 'provider': narration.provider or narration.origin,
                      'origin': narration.origin, 'synthetic_voice': synthetic,
                      'disclosure': SYNTHETIC_DISCLOSURE if synthetic else '', 'license_status': narration.license_status,
                      'word_timing': compiled.word_timing},
        'provenance': compiled.provenance, 'rights_review': dict(rights, copyright_note=COPYRIGHT_NOTE,
                                                                 altered_or_synthetic_content=synthetic),
        'rights_review_status': rights['status'], 'metadata_source': metadata_source,
        'caption_renderer': compiled.caption_renderer, 'graphics_cards': compiled.cards,
        'caption_timing': 'Word timestamps from the narration provider.' if compiled.word_timing == 'provider' else
                          'Approximate caption timing (proportional to the text).' if compiled.word_timing == 'approximate' else
                          f'Caption timing from {compiled.word_timing}.',
        'warnings': compiled.warnings + result.warnings,
        'transformations': ['Original script narrated over original motion graphics and licensed or owned media',
                            'Captions from narration timing', 'Loudness normalisation'],
        'basis': 'Technical QA and editorial heuristics; not a view or virality prediction.',
        'file_size': output.stat().st_size,
    }
    with transaction.atomic():
        short, _ = GeneratedShort.objects.update_or_create(job=job, short_number=number, defaults=dict(
            start_time=0, end_time=duration, duration=duration, title=title, description=description,
            hashtags=hashtags, transcript=script.full_text, caption_file=relative(srt), video_file=relative(output),
            thumbnail_file=relative(thumbnail), quality_score=float((script.quality_report or {}).get('score') or 0),
            quality_report=report, rights_review_status=rights['status'], kind='story', project=project,
            script=script, timeline=compiled.timeline.model_dump(mode='json'), qa_status=qa_status))
        short.segments.all().delete()
        lang = LANG_CODES.get(script.language, 'und')
        TranscriptSegment.objects.bulk_create([
            TranscriptSegment(short=short, start_ms=int(round(s * 1000)), end_ms=max(int(round(e * 1000)), int(round(s * 1000)) + 1),
                              original_text=text, detected_language=lang)
            for s, e, text in compiled.groups])
    ctx.mark(f'video_{number}', tick)
    return short


def render(ctx, project):
    job = ctx.job
    ids = job.settings.get('script_ids') or []
    scripts = list(GeneratedScript.objects.filter(pk__in=ids, project=project).select_related('narration_asset', 'concept'))
    scripts.sort(key=lambda s: ids.index(str(s.pk)))
    preflight(project, scripts)
    videos, failures = [], []
    for number, script in enumerate(scripts, 1):
        try:
            videos.append(make_video(ctx, project, script, number, len(scripts)))
        except StudioError as exc:
            if exc.code in ('processing_timeout', 'job_timeout', 'disk_full'):
                raise
            failures.append({'script_id': str(script.pk), 'number': number, 'error': exc.code, 'message': exc.message})
            ctx.warnings.append(f'Video {number}: {exc.message}')
    if not videos:
        first = failures[0]
        raise StudioError(first['error'], first['message'])
    flagged = [v for v in videos if v.qa_status != 'passed' or v.rights_review_status == 'manual_review_required']
    status = 'needs_review' if flagged or failures else 'completed'
    finish(ctx, status, f'{len(videos)} story video(s) ready' + (' for review' if status == 'needs_review' else ''),
           videos=[str(v.pk) for v in videos], failures=failures)


STEPS = {'analyze': analyze, 'write': write, 'render': render}


def new_job(project, step, **settings_):
    return GenerationJob.objects.create(project=project, pipeline='story', step=step, owner=project.owner,
                                        source=project.source, settings=settings_,
                                        stage='Waiting for worker', status='queued')

