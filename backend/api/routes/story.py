"""AI Story Studio API (/api/v1/story): projects, source intelligence, concepts, scripts, narration, storyboard, render.

Owner-scoped (session cookie auth), CSRF-protected like every other route. Long steps (analyze, concepts, scripts,
regenerate, render) are GenerationJob rows with pipeline='story' polled through GET /api/v1/jobs/{id}. Narration and
storyboarding are synchronous. With no text provider every AI step answers 400 ``text_provider_unavailable`` with the
provider reasons; the manual script path keeps narrate -> storyboard -> render working.
"""
import logging
import shutil
import unicodedata
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router
from ninja.errors import HttpError

from ..models import GeneratedScript, GenerationJob, GenerationProject, MediaAsset, SourceVideo, StoryboardScene
from ..pipeline import story as pipeline
from ..pipeline import story_tasks
from ..pipeline.states import TERMINAL
from ..providers import ProviderRateLimited
from ..services import assets as asset_svc
from ..services import narration as narration_svc
from ..services.media import StudioError, disk_check, validate_youtube
from ..story import intelligence as intel
from ..story import scoring, storyboard, scripts as writer
from ..story.schemas import (AttachNarrationIn, ClaimEvidenceIn, ConceptSelectIn, ManualScriptIn, NarrateIn, ProjectIn,
                             ProjectUpdateIn, RenderIn, SceneEditIn, ScriptEditIn, ScriptsGenerateIn, StoryboardIn,
                             TARGET_DURATIONS)
from ..story.scripts import TextProviderUnavailable
from .base import api
from .narration import rate_limited
from .serializers import job_data, short_data

log = logging.getLogger(__name__)
router = Router()
GLOBAL_ACTIVE_LIMIT = 10
HOURLY_STORY_JOBS = 30


@api.exception_handler(TextProviderUnavailable)
def text_provider_unavailable(request, exc):
    return api.create_response(request, {'error': exc.code, 'message': exc.message, 'reasons': exc.reasons}, status=400)


# ------------------------------------------------------------------------------------- lookups

def owned_project(request, project_id):
    return get_object_or_404(GenerationProject.objects.select_related('source'), pk=project_id, owner=request.auth,
                             kind='story')


def owned_script(request, script_id):
    return get_object_or_404(GeneratedScript.objects.select_related('project__source', 'concept', 'narration_asset'),
                             pk=script_id, project__owner=request.auth, project__kind='story')


def owned_scene(request, scene_id):
    return get_object_or_404(StoryboardScene.objects.select_related('script__project__source', 'script__narration_asset', 'asset'),
                             pk=scene_id, script__project__owner=request.auth)


def capacity(owner):
    disk_check(settings.MEDIA_ROOT)
    if GenerationJob.objects.exclude(status__in=TERMINAL).count() >= GLOBAL_ACTIVE_LIMIT:
        raise HttpError(429, 'The studio queue is full. Please wait.')
    if GenerationJob.objects.filter(owner=owner, pipeline='story').exclude(status__in=TERMINAL).exists():
        raise HttpError(429, 'A story step is already running. Wait for it to finish or cancel it.')
    if GenerationJob.objects.filter(owner=owner, pipeline='story',
                                    created_at__gte=timezone.now() - timedelta(hours=1)).count() >= HOURLY_STORY_JOBS:
        raise HttpError(429, 'Hourly story step limit reached. Try again later.')


def start(project, step, **job_settings):
    capacity(project.owner)
    job = pipeline.new_job(project, step, **job_settings)
    story_tasks.dispatch(job)
    job.refresh_from_db()
    return 202, job_data(job)


def clean_text(text):
    text = unicodedata.normalize('NFC', text or '')
    return ''.join(ch for ch in text if ch in '\n\t' or unicodedata.category(ch)[0] != 'C').strip()


# ------------------------------------------------------------------------------------- serializers

def asset_summary(asset):
    if asset is None:
        return None
    base = f'/api/v1/assets/{asset.pk}'
    return {'id': str(asset.pk), 'title': asset.title, 'kind': asset.kind, 'origin': asset.origin,
            'license_status': asset.license_status,
            'license_label': asset_svc.LICENSE_LABELS.get(asset.license_status, asset.license_status),
            'review_required': asset_svc.review_required(asset), 'attribution_text': asset.attribution_text,
            'duration': asset.duration, 'width': asset.width, 'height': asset.height, 'provider': asset.provider,
            'file_url': f'{base}/file', 'thumbnail_url': f'{base}/thumbnail' if asset_svc.has_thumbnail(asset) else None,
            'third_party_source_footage': asset.origin == 'source_ingest', 'deleted': asset.deleted_at is not None}


def scene_data(scene):
    spec = scene.graphic_spec or {}
    asset = scene.asset
    status = asset.license_status if asset is not None else 'generated'
    return {'id': str(scene.pk), 'order': scene.order, 'start': scene.start, 'end': scene.end,
            'narration_text': scene.narration_text, 'visual_type': scene.visual_type, 'asset': asset_summary(asset),
            'asset_in': scene.asset_in, 'asset_out': scene.asset_out, 'card': spec.get('card'),
            'claim_id': spec.get('claim_id'), 'attribution': spec.get('attribution'), 'transition': scene.transition,
            'rights_status': scene.rights_status,
            'license_badge': {'status': status, 'label': asset_svc.LICENSE_LABELS.get(status, 'Original graphic'),
                              'review_required': status in ('unknown', 'permission_pending', 'restricted'),
                              'blocked': status == 'restricted'}}


def narration_data(asset):
    if asset is None:
        return None
    meta = asset.metadata or {}
    return dict(asset_summary(asset), word_timing=meta.get('timing') or ('none' if meta.get('manual') else 'approximate'),
                voice=meta.get('voice'), synthetic_voice=asset.origin == 'generated_tts',
                commercial_use=meta.get('commercial_use'))


def script_data(script, with_scenes=True):
    sections = dict(script.sections or {})
    title = sections.pop('title', '')
    quotes = sections.pop('quotes', [])
    scenes = list(script.scenes.select_related('asset').order_by('order')) if with_scenes else []
    return {'id': str(script.pk), 'project_id': str(script.project_id),
            'concept_id': str(script.concept_id) if script.concept_id else None, 'manual': script.concept_id is None,
            'version': script.version, 'title': title, 'language': script.language,
            'narration_style': script.narration_style, 'target_duration': script.target_duration,
            'sections': sections, 'quotes': quotes, 'full_text': script.full_text, 'word_count': script.word_count,
            'estimated_duration': script.estimated_duration, 'quality_report': script.quality_report,
            'originality_overlap': script.originality_overlap, 'attempts': script.attempts,
            'attempts_left': (writer.MAX_TOTAL_ATTEMPTS - script.attempts) if script.concept_id else None,
            'status': script.status, 'narration': narration_data(script.narration_asset), 'voice': script.voice,
            'provider': script.provider, 'scenes': [scene_data(s) for s in scenes],
            'storyboard_ready': bool(scenes), 'created_at': script.created_at.isoformat()}


def concept_data(concept):
    return {'id': str(concept.pk), 'index': concept.index, 'angle': concept.angle, 'title': concept.title,
            'logline': concept.logline, 'hook_idea': concept.hook_idea, 'key_points': concept.key_points,
            'claim_ids': concept.claim_ids, 'diversity_score': concept.diversity_score,
            'quality_score': concept.quality_score, 'selected': concept.selected, 'revision': concept.revision,
            'provider': concept.provider, 'has_script': concept.scripts.exists()}


def project_summary(project):
    inputs = project.inputs or {}
    return {'id': str(project.pk), 'title': project.title or 'Untitled story', 'language': project.language,
            'status': project.status, 'content_format': inputs.get('content_format'),
            'narration_style': inputs.get('narration_style'), 'target_duration': inputs.get('target_duration'),
            'video_count': inputs.get('video_count', 3), 'url': inputs.get('url') or '',
            'has_transcript': bool(project.source_notes), 'created_at': project.created_at.isoformat(),
            'updated_at': project.updated_at.isoformat(),
            'counts': {'concepts': project.concepts.count(), 'scripts': project.scripts.count(),
                       'videos': project.outputs.filter(kind='story').count()}}


def text_provider_status():
    try:
        adapter = writer.text_provider()
        return {'available': True, 'provider': writer.provider_name(adapter), 'reasons': []}
    except TextProviderUnavailable as exc:
        return {'available': False, 'provider': None, 'reasons': exc.reasons}


def project_detail(project):
    inputs = project.inputs or {}
    source = project.source
    jobs = list(project.jobs.order_by('-created_at')[:10])
    active = next((j for j in jobs if j.status not in TERMINAL), None)
    videos = project.outputs.filter(kind='story').select_related('job').prefetch_related('segments') \
        .order_by('-job__created_at', 'short_number')
    return dict(project_summary(project),
                inputs={k: inputs.get(k) for k in ('url', 'content_format', 'narration_style', 'target_duration',
                                                   'video_count', 'music_asset_id', 'theme', 'caption_preset',
                                                   'resolution')},
                source={'url': source.source_url, 'title': source.title, 'rights_declaration': source.rights_declaration,
                        'permission_reference': source.permission_reference} if source else None,
                transcript_chars=len(project.source_notes or ''),
                intelligence=intel.public(project.intelligence),
                concepts=[concept_data(c) for c in project.concepts.order_by('index')],
                scripts=[script_data(s) for s in project.scripts.select_related('narration_asset').order_by('created_at')],
                videos=[dict(short_data(v), script_id=str(v.script_id) if v.script_id else None, job_id=str(v.job_id))
                        for v in videos],
                active_job=job_data(active) if active else None,
                jobs=[{'id': str(j.pk), 'step': j.step, 'op': (j.settings or {}).get('op'), 'status': j.status,
                       'error_code': j.error_code, 'error_message': j.error_message,
                       'created_at': j.created_at.isoformat()} for j in jobs],
                text_provider=text_provider_status(),
                limits={'max_attempts_per_round': writer.MAX_ATTEMPTS_PER_ROUND,
                        'max_total_attempts': writer.MAX_TOTAL_ATTEMPTS, 'quality_threshold': scoring.THRESHOLD,
                        'source_clip_max_share': storyboard.MAX_SOURCE_CLIP_SHARE,
                        'wpm': scoring.WPM, 'target_durations': TARGET_DURATIONS})


# ------------------------------------------------------------------------------------- projects

@router.post('/v1/story/projects', response={201: dict})
def create_project(request, data: ProjectIn):
    url = (data.url or '').strip()
    transcript = clean_text(data.transcript)
    if not url and not transcript:
        raise StudioError('input_required', 'Provide a YouTube URL, a pasted transcript/notes, or both.')
    source = None
    if url:
        if not data.rights_declaration:
            raise StudioError('rights_required', 'Confirm you own the source or have permission to use it before '
                              'analysing a URL. The rights declaration is required.')
        url = validate_youtube(url)
    with transaction.atomic():
        if url:
            source = SourceVideo.objects.create(source_url=url, rights_declaration=True,
                                                permission_reference=data.permission_reference.strip(),
                                                license_status='verification_needed')
        project = GenerationProject.objects.create(
            owner=request.auth, kind='story', title=clean_text(data.title)[:300], language=data.language, source=source,
            source_notes=transcript, status='draft',
            inputs={'url': url, 'content_format': data.content_format, 'narration_style': data.narration_style,
                    'target_duration': data.target_duration, 'video_count': data.video_count, 'resolution': '720',
                    'theme': 'gaming_hype', 'caption_preset': 'clean_commentary', 'music_asset_id': None})
    return 201, project_detail(project)


@router.get('/v1/story/projects')
def list_projects(request):
    projects = GenerationProject.objects.filter(owner=request.auth, kind='story').order_by('-created_at')[:50]
    return {'projects': [project_summary(p) for p in projects]}


@router.get('/v1/story/projects/{project_id}')
def get_project(request, project_id: uuid.UUID):
    return project_detail(owned_project(request, project_id))


@router.put('/v1/story/projects/{project_id}')
def update_project(request, project_id: uuid.UUID, data: ProjectUpdateIn):
    project = owned_project(request, project_id)
    inputs = dict(project.inputs or {})
    if data.title is not None:
        project.title = clean_text(data.title)[:300]
    if data.music_asset_id is not None:
        if data.music_asset_id == '':
            inputs['music_asset_id'] = None
        else:
            asset = MediaAsset.objects.filter(pk=data.music_asset_id, owner=request.auth, deleted_at__isnull=True).first() \
                if _is_uuid(data.music_asset_id) else None
            if asset is None or asset.kind not in ('music', 'audio'):
                raise StudioError('invalid_music', 'Background music must be one of your music or audio library assets.')
            if asset.license_status == 'restricted':
                raise StudioError('restricted_asset', 'This music is restricted and cannot be used.')
            inputs['music_asset_id'] = str(asset.pk)
    for key in ('theme', 'caption_preset', 'video_count'):
        value = getattr(data, key)
        if value is not None:
            inputs[key] = value
    project.inputs = inputs
    project.save(update_fields=['title', 'inputs', 'updated_at'])
    return project_detail(project)


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


@router.delete('/v1/story/projects/{project_id}', response={204: None})
def delete_project(request, project_id: uuid.UUID):
    project = owned_project(request, project_id)
    jobs = list(project.jobs.values_list('pk', flat=True))
    GenerationJob.objects.filter(pk__in=jobs).exclude(status__in=TERMINAL).update(
        status='cancelled', stage='Cancelled', completed_at=timezone.now())
    source = project.source
    with transaction.atomic():
        project.delete()
        if source is not None and not source.projects.exists() and not GenerationJob.objects.filter(source=source).exists():
            source.delete()
    for job_id in jobs:
        shutil.rmtree(settings.MEDIA_ROOT/'jobs'/str(job_id), ignore_errors=True)
    return 204, None


# ------------------------------------------------------------------------------------- intelligence

@router.post('/v1/story/projects/{project_id}/analyze', response={202: dict})
def analyze(request, project_id: uuid.UUID):
    project = owned_project(request, project_id)
    if project.source and not project.source.rights_declaration:
        raise StudioError('rights_required', 'The source has no rights declaration.')
    return start(project, 'analyze')


@router.put('/v1/story/projects/{project_id}/claims/{claim_id}')
def attach_evidence(request, project_id: uuid.UUID, claim_id: str, data: ClaimEvidenceIn):
    project = owned_project(request, project_id)
    with transaction.atomic():
        project = GenerationProject.objects.select_for_update().get(pk=project.pk)
        intelligence = dict(project.intelligence or {})
        claims = list(intelligence.get('claims') or [])
        claim = next((c for c in claims if c['id'] == claim_id), None)
        if claim is None:
            raise HttpError(404, 'Claim not found.')
        if claim.get('status') != 'verified':
            claim['previous_status'] = claim.get('status')
        claim['status'] = 'verified'
        claim['evidence'] = {'url': data.evidence_url, 'note': data.evidence_note,
                             'attached_at': timezone.now().isoformat(),
                             'basis': 'Evidence supplied by the creator; not checked automatically.'}
        intelligence['claims'] = claims
        project.intelligence = intelligence
        project.save(update_fields=['intelligence', 'updated_at'])
    return claim


@router.delete('/v1/story/projects/{project_id}/claims/{claim_id}/evidence')
def remove_evidence(request, project_id: uuid.UUID, claim_id: str):
    project = owned_project(request, project_id)
    with transaction.atomic():
        project = GenerationProject.objects.select_for_update().get(pk=project.pk)
        intelligence = dict(project.intelligence or {})
        claim = next((c for c in intelligence.get('claims') or [] if c['id'] == claim_id), None)
        if claim is None:
            raise HttpError(404, 'Claim not found.')
        if claim.get('status') == 'verified':
            claim['status'] = claim.pop('previous_status', None) or 'inferred'
        claim['evidence'] = None
        project.intelligence = intelligence
        project.save(update_fields=['intelligence', 'updated_at'])
    return claim


# ------------------------------------------------------------------------------------- concepts

@router.post('/v1/story/projects/{project_id}/concepts/generate', response={202: dict})
def generate_concepts(request, project_id: uuid.UUID):
    project = owned_project(request, project_id)
    if not (project.intelligence or {}).get('claims'):
        raise StudioError('analyze_first', 'Analyse the source first; concepts are built on its claims.')
    writer.text_provider()
    return start(project, 'write', op='concepts')


@router.post('/v1/story/projects/{project_id}/concepts/select')
def select_concepts(request, project_id: uuid.UUID, data: ConceptSelectIn):
    project = owned_project(request, project_id)
    ids = list(dict.fromkeys(data.concept_ids))
    concepts = list(project.concepts.filter(pk__in=[i for i in ids if _is_uuid(i)]))
    if len(concepts) != len(ids):
        raise StudioError('invalid_concepts', 'Every concept id must belong to this project.')
    limit = int((project.inputs or {}).get('video_count', 3))
    if len(ids) > limit:
        raise StudioError('too_many_concepts', f'Select at most {limit} concept(s) (the project video count).')
    with transaction.atomic():
        project.concepts.update(selected=False)
        project.concepts.filter(pk__in=ids).update(selected=True)
    return {'concepts': [concept_data(c) for c in project.concepts.order_by('index')]}


# ------------------------------------------------------------------------------------- scripts

@router.post('/v1/story/projects/{project_id}/scripts/generate', response={202: dict})
def generate_scripts(request, project_id: uuid.UUID, data: ScriptsGenerateIn = None):
    project = owned_project(request, project_id)
    if not project.concepts.filter(selected=True).exists():
        raise StudioError('no_concepts_selected', 'Select at least one concept before writing scripts.')
    writer.text_provider()
    return start(project, 'write', op='scripts', force=bool(data and data.force))


def _sections_from(data):
    if data.sections is not None:
        sections = data.sections.model_dump()
    else:
        sections = writer.sections_from_text(clean_text(data.full_text))
    return {k: (clean_text(v) if isinstance(v, str) else [clean_text(x) for x in v if clean_text(x)])
            for k, v in sections.items()}


@router.post('/v1/story/projects/{project_id}/scripts/manual', response={201: dict})
def manual_script(request, project_id: uuid.UUID, data: ManualScriptIn):
    project = owned_project(request, project_id)
    if project.scripts.count() >= 12:
        raise StudioError('script_limit', 'This project already has 12 scripts; delete the project or edit an existing script.')
    sections = _sections_from(data)
    report = writer.score_for_project(project, sections, language=data.language)
    lo, hi = scoring.target_window((project.inputs or {}).get('target_duration', '30_45'))
    script = GeneratedScript(project=project, concept=None, version=1, language=data.language,
                             narration_style=(project.inputs or {}).get('narration_style', ''), target_duration=hi,
                             attempts=0, provider='manual')
    writer.apply_report(script, sections, report, title=clean_text(data.title))
    script.save()
    return 201, script_data(script)


@router.put('/v1/story/scripts/{script_id}')
def edit_script(request, script_id: uuid.UUID, data: ScriptEditIn):
    script = owned_script(request, script_id)
    current = dict(script.sections or {})
    title = clean_text(data.title) if data.title is not None else current.get('title', '')
    if data.sections is not None or data.full_text is not None:
        sections = _sections_from(data)
        sections['quotes'] = current.get('quotes', [])
    else:
        sections = {k: v for k, v in current.items() if k != 'title'}
    old_text = script.full_text
    report = writer.score_for_project(script.project, sections, language=script.language,
                                      quotes=sections.get('quotes') or ())
    writer.apply_report(script, sections, dict(report, edited_by_user=True), title=title)
    if script.full_text != old_text:
        script.narration_asset = None
        script.scenes.all().delete()
    script.save()
    return script_data(script)


@router.post('/v1/story/scripts/{script_id}/regenerate', response={202: dict})
def regenerate_script(request, script_id: uuid.UUID):
    script = owned_script(request, script_id)
    if script.concept_id is None:
        raise StudioError('manual_script', 'Manual scripts are not regenerated by AI; edit them directly.')
    left = writer.MAX_TOTAL_ATTEMPTS - script.attempts
    if left <= 0:
        raise StudioError('regeneration_limit', f'This script has used all {writer.MAX_TOTAL_ATTEMPTS} generation attempts. '
                          'Edit it by hand instead.')
    writer.text_provider()
    status, job = start(script.project, 'write', op='regenerate', script_id=str(script.pk))
    return status, dict(job, attempts_left=left, attempts_this_round=min(writer.MAX_ATTEMPTS_PER_ROUND, left))


@router.post('/v1/story/scripts/{script_id}/approve')
def approve_script(request, script_id: uuid.UUID, override_quality: bool = False):
    script = owned_script(request, script_id)
    report = script.quality_report or {}
    if not (report.get('gates') or {}).get('originality', True):
        raise StudioError('originality_failed', 'This script is too close to the source text. Rewrite it before approving.')
    if not report.get('passed') and not override_quality:
        return _quality_error(report)
    script.status = 'approved'
    script.save(update_fields=['status'])
    return script_data(script)


def _quality_error(report):
    raise StudioError('quality_below_threshold', 'The script scored {:.2f} (threshold {:.2f}): {}. Fix it, or approve with '
                      '?override_quality=true after reviewing it yourself.'.format(
                          float(report.get('score') or 0), scoring.THRESHOLD, '; '.join((report.get('reasons') or [])[:3])))


# ------------------------------------------------------------------------------------- narration

def _set_narration(script, asset, voice):
    script.narration_asset = asset
    script.voice = voice
    script.save(update_fields=['narration_asset', 'voice'])
    script.scenes.all().delete()


@router.post('/v1/story/scripts/{script_id}/narrate', response={200: dict, 429: dict})
def narrate(request, script_id: uuid.UUID, data: NarrateIn):
    script = owned_script(request, script_id)
    if not script.full_text.strip():
        raise StudioError('script_empty', 'The script has no text to narrate.')
    try:
        asset = narration_svc.synthesize_narration(request.auth, script.full_text, script.language, data.voice_id,
                                                   data.speed, provider=data.provider)
    except ProviderRateLimited as exc:
        return rate_limited(exc)
    _set_narration(script, asset, {'provider': asset.provider, 'voice_id': data.voice_id, 'speed': data.speed})
    return 200, script_data(script)


@router.post('/v1/story/scripts/{script_id}/narration')
def attach_narration(request, script_id: uuid.UUID, data: AttachNarrationIn):
    script = owned_script(request, script_id)
    asset = MediaAsset.objects.filter(pk=data.asset_id, owner=request.auth, deleted_at__isnull=True).first() \
        if _is_uuid(data.asset_id) else None
    if asset is None:
        raise StudioError('asset_unavailable', 'Narration asset not found in your library.')
    asset = narration_svc.attach_manual_narration(request.auth, asset)
    _set_narration(script, asset, {'provider': asset.provider or asset.origin, 'asset_id': str(asset.pk)})
    return script_data(script)


# ------------------------------------------------------------------------------------- storyboard

@router.post('/v1/story/scripts/{script_id}/storyboard')
def build_storyboard(request, script_id: uuid.UUID, data: StoryboardIn = None):
    script = owned_script(request, script_id)
    data = data or StoryboardIn()
    specs, report = storyboard.plan(script, request.auth, data.asset_ids, data.use_source_clips)
    scenes = storyboard.save(script, specs, request.auth)
    return {'script_id': str(script.pk), 'scenes': [scene_data(s) for s in scenes], 'report': report}


@router.get('/v1/story/scripts/{script_id}/scenes')
def list_scenes(request, script_id: uuid.UUID):
    script = owned_script(request, script_id)
    scenes = list(script.scenes.select_related('asset').order_by('order'))
    duration = script.narration_asset.duration if script.narration_asset else 0
    clip = sum(s.end - s.start for s in scenes if s.visual_type == 'source_clip')
    return {'script_id': str(script.pk), 'scenes': [scene_data(s) for s in scenes],
            'source_clip_share': round(clip / duration, 3) if duration else 0.0}


@router.put('/v1/story/scenes/{scene_id}')
def edit_scene(request, scene_id: uuid.UUID, data: SceneEditIn):
    scene = owned_scene(request, scene_id)
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        raise StudioError('nothing_to_update', 'Send at least one scene field to change.')
    scene = storyboard.edit_scene(scene, changes, request.auth)
    return scene_data(scene)


# ------------------------------------------------------------------------------------- render

@router.post('/v1/story/projects/{project_id}/render', response={202: dict})
def render(request, project_id: uuid.UUID, data: RenderIn = None):
    project = owned_project(request, project_id)
    data = data or RenderIn()
    if data.script_ids:
        ids = list(dict.fromkeys(data.script_ids))
        scripts = list(project.scripts.filter(pk__in=[i for i in ids if _is_uuid(i)]).select_related('narration_asset'))
        if len(scripts) != len(ids):
            raise StudioError('invalid_scripts', 'Every script id must belong to this project.')
        scripts.sort(key=lambda s: ids.index(str(s.pk)))
    else:
        scripts = list(project.scripts.filter(status='approved').select_related('narration_asset').order_by('created_at'))
    if len(scripts) > 3:
        raise StudioError('too_many_scripts', 'Render at most three scripts at a time.')
    pipeline.preflight(project, scripts)
    return start(project, 'render', script_ids=[str(s.pk) for s in scripts])
