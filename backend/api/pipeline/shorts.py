"""Per-clip Shorts stages: transcript refinement -> captions -> timeline -> render + QA -> metadata -> persist.

Every Short is compiled into a ``Timeline`` (``editing.shorts_compiler``) and rendered with
``editing.render.render_timeline``. With ``preset='none'`` and every effect switch off, the
timeline is the legacy single-clip render (same crop, grade, fades and loudness chain).
"""
import shutil
import time

from django.utils import timezone

from ..editing import presets
from ..editing.render import render_timeline
from ..editing.shorts_compiler import compile_candidate, media_relative, snap_candidate
from ..models import GeneratedShort, GenerationJob, MediaAsset, TranscriptSegment
from ..providers import Capability
from ..services.captions import local_cues
from ..services.languages import caption_text, label_cues
from ..services.media import StudioError, disk_check, probe, run
from .context import relative, update
from .metadata import clip_metadata
from .rights import build_rights_review


def render_shorts(ctx):
    selected = ctx.selected
    tick = time.monotonic()
    options = presets.resolve(ctx.job.settings)
    secondary = load_secondary(ctx, options)
    if not options.legacy and ctx.cues:
        snap_selected(ctx, selected)
    for number, candidate in enumerate(selected, 1):
        make_short(ctx, number, candidate, len(selected), options, secondary)
    ctx.mark('rendering_and_qa', tick)


def finish_job(ctx, provider_calls):
    job = ctx.job
    job.analysis.update(warnings=ctx.warnings, timings=ctx.timings, inference_calls=provider_calls,
        total_seconds=round(time.monotonic()-ctx.started, 2), disk_bytes=disk_check(ctx.root))
    # Human rights review is always needed; a declaration is not proof of ownership.
    update(job, 'needs_review', 'Ready for final review', 100)
    GenerationJob.objects.filter(pk=job.pk).update(analysis=job.analysis, completed_at=timezone.now())


def snap_selected(ctx, selected):
    """Move each selected window onto sentence/scene boundaries when that keeps them distinct."""
    for i, candidate in enumerate(selected):
        others = [c for j, c in enumerate(selected) if j != i]
        snapped = snap_candidate(candidate, ctx.cues, ctx.signals, ctx.info['duration'], others)
        if snapped:
            start, end, reasons = snapped
            candidate['bounds_snap'] = dict(original=[candidate['start'], candidate['end']], reasons=reasons)
            candidate['start'], candidate['end'] = start, end


def load_secondary(ctx, options):
    """The owner's MediaAsset used as the second split-screen panel (re-checked at render time)."""
    if not options.secondary_asset_id:
        return None
    if options.composition != 'split_screen':
        ctx.warnings.append('A secondary clip was supplied but the composition is not split screen; it was not used.')
        return None
    from ..services.assets import asset_path
    asset = MediaAsset.objects.filter(pk=options.secondary_asset_id, owner=ctx.job.owner, deleted_at__isnull=True).first()
    if asset is None:
        raise StudioError('asset_not_found', 'The secondary split-screen clip is no longer in your media library.')
    if asset.license_status == 'restricted':
        raise StudioError('asset_restricted', 'The secondary clip is marked restricted and cannot be used in a render.')
    if asset.kind not in ('video', 'image'):
        raise StudioError('asset_kind', 'The secondary split-screen panel must be a video or image asset.')
    path = asset_path(asset)
    duration, width, height, has_audio = asset.duration, asset.width, asset.height, False
    if asset.kind == 'video' and not (duration and width and height):
        info = probe(path)
        duration, width, height, has_audio = info['duration'], info['width'], info['height'], info['has_audio']
    if asset.license_status in ('unknown', 'permission_pending'):
        ctx.warnings.append('The secondary split-screen clip has unknown or pending licence status; outputs need rights review.')
    return dict(path=media_relative(path), duration=float(duration or 3600), width=width, height=height, has_audio=has_audio,
                license_status=asset.license_status, asset_id=str(asset.pk), kind=asset.kind, title=asset.title)


def clip_transcript(ctx, number, candidate):
    job, info = ctx.job, ctx.info
    clip_cues = local_cues(ctx.cues, candidate['start'], candidate['end'])
    caption_timing = job.analysis['transcription_timing']
    # Groq clip ASR now passes the policy gate (free tier, cooldown, GROQ_ENABLED) and is ledger-counted.
    if info['has_audio'] and ctx.usable('groq', Capability.ASR):
        improved = ctx.hosted('groq').transcribe_clip(ctx.source, ctx.folders['audio'], candidate['start'],
                                                      candidate['end']-candidate['start'], number)
        if improved:
            clip_cues = label_cues(improved)
            caption_timing = ('Groq word timestamps used for caption phrases.' if
                              improved[0]['timing'] == 'groq_word' else
                              'Groq segment timestamps used; phrase subdivisions are approximate.')
    return clip_cues, caption_timing


def translate_cues(ctx, clip_cues):
    """Translate/filter cues for the subtitle mode; never invents a missing translation."""
    job, warnings, provider = ctx.job, ctx.warnings, ctx.provider
    subtitle_mode = job.settings.get('subtitle_mode', 'original')
    if clip_cues and subtitle_mode != 'original' and provider is not None and provider.availability(Capability.TRANSLATION).available:
        try:
            translated = provider.translate(clip_cues)
            for cue, pair in zip(clip_cues, translated):
                cue['translated_english'] = pair.english
                cue['translated_hindi'] = pair.hindi
        except StudioError as exc:
            warnings.append(exc.message)
    if clip_cues and subtitle_mode != 'original':
        missing = [cue for cue in clip_cues if not caption_text(cue, subtitle_mode)]
        if missing:
            warnings.append(f'{subtitle_mode.title()} subtitles unavailable for {len(missing)} segments; no translation was invented. Configure an approved translation provider or edit the transcript.')
        clip_cues = [cue for cue in clip_cues if caption_text(cue, subtitle_mode)]
    return clip_cues


def render_clip(ctx, number, compiled, total):
    """Render the timeline (one bounded repair attempt inside render_timeline), cut a thumbnail."""
    job, folders = ctx.job, ctx.folders
    output = folders['renders']/f'{number}.mp4'
    workdir = folders['renders']/f'.work-{number}'
    update(job, 'rendering', f'Rendering Short {number} of {total}', 48+(number-1)*15)
    try:
        result = render_timeline(compiled.timeline, output, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    update(job, 'validating', f'Quality checking Short {number}', 55+(number-1)*15)
    duration = compiled.timeline.duration
    thumbnail = folders['thumbnails']/f'{number}.jpg'
    run(['ffmpeg', '-nostdin', '-y', '-ss', min(1, duration/2), '-i', output, '-frames:v', '1', '-vf', 'scale=270:480', thumbnail])
    return output, thumbnail, result.report, duration


def _transformations(options, compiled, info):
    summary, report = compiled.summary, compiled.report
    composition = summary['composition']
    items = ['Precise trim' if not report['pacing']['removed_seconds']
             else f"Dead air removed ({report['pacing']['removed_seconds']:.1f}s)",
             f'9:16 {composition.replace("_", " ")} composition' if not options.legacy
             else '9:16 ' + ('center' if composition == 'center' else 'preserve') + ' layout',
             {'restrained': 'Restrained color adjustment', 'punchy': 'Punchy color grade',
              'filmic': 'Filmic color grade', 'none': 'No color grade'}[options.grade],
             'Audio normalization and peak limiting' if info['has_audio'] else 'No source audio']
    if summary['punch_ins']:
        items.append(f"{summary['punch_ins']} punch-in zooms (cut)")
    if summary['hook_overlay']:
        items.append('Hook title overlay (words from the clip transcript)')
    if compiled.timeline.captions or any(o.kind == 'caption' for o in compiled.timeline.overlays):
        items.append(f"{'Animated' if summary['caption_renderer'] == 'ass' else 'Image'} phrase captions "
                     f"({'word timings' if report.get('caption_word_timing') else 'approximate subdivisions'})")
    else:
        items.append('No captions available')
    return items


def make_short(ctx, number, candidate, total, options=None, secondary=None):
    job, info, provider, warnings = ctx.job, ctx.info, ctx.provider, ctx.warnings
    options = options or presets.resolve(job.settings)
    s = job.source
    progress = 44+(number-1)*15
    update(job, 'editing', f'Preparing Short {number} of {total}', progress)
    clip_cues, caption_timing = clip_transcript(ctx, number, candidate)
    text = ' '.join(c['text'] for c in clip_cues)
    editorial = candidate.get('editorial')
    if not editorial and text and provider is not None and provider.availability(Capability.TEXT).available:
        try:
            editorial = provider.analyze(text)
        except StudioError as exc:
            if exc.message not in warnings:
                warnings.append(exc.message)
    commentary = None
    if job.settings.get('mode') == 'original_analysis':
        if not text.strip():
            raise StudioError('commentary_needs_transcript','Original analysis needs source speech or permitted subtitles.')
        commentary = provider.commentary(text)
    update(job, 'captioning', f'Building captions for Short {number}', 46+(number-1)*15)
    clip_cues = translate_cues(ctx, clip_cues)
    compiled = compile_candidate(
        ctx.source, info, candidate, clip_cues, ctx.signals, options, workdir=ctx.folders['captions']/f'edit-{number}',
        number=number, source_license=s.license_status, secondary=secondary, commentary=commentary,
        legacy_style=job.settings.get('caption_style', 'minimal') if options.legacy else None,
        caption_folder=ctx.folders['captions'],
        checkpoint=lambda message: update(job, 'editing', f'Short {number}: {message}', progress))
    for warning in compiled.warnings:
        if warning not in warnings:
            warnings.append(warning)
    output, thumbnail, report, duration = render_clip(ctx, number, compiled, total)
    title, description, hashtags, groq_metadata = clip_metadata(text, editorial, s.title, number,
                                                                  generate=ctx.hosted('groq').metadata)
    report['rights_review'] = build_rights_review(s, ctx.metadata, info, commentary)
    provenance = report.get('provenance') or {}
    report.update(highlight_score=candidate['score'], components=candidate['components'],
        decision=editorial['reason'] if editorial else candidate['reason'],
        transformations=_transformations(options, compiled, info),
        title_candidates=editorial['title_candidates'] if editorial else [title],
        metadata_source='groq' if groq_metadata else 'existing',
        file_size=output.stat().st_size, caption_timing=caption_timing,
        original_commentary=commentary, timeline_summary=compiled.summary,
        pacing=compiled.report['pacing'], punch_ins=compiled.report['punch_ins'], crop=compiled.report['crop'],
        crop_mode=compiled.report['crop'].get('mode'), reframe_label=compiled.summary['reframe_label'],
        hook_overlay=compiled.report['hook_overlay'], hook_overlay_source=compiled.report['hook_overlay']['source'],
        caption_renderer=compiled.summary['caption_renderer'], caption_preset=options.caption_preset,
        rights_summary=dict(
            provenance=provenance, source_license_status=s.license_status,
            secondary_asset=compiled.report['crop'].get('secondary'),
            note=('Rights for each input are assessed separately; a secondary clip never clears rights for the '
                  'source footage. Human review is required before publishing.')),
        bounds_snap=candidate.get('bounds_snap'))
    update(job, 'validating', f'Saving validated Short {number}', 56+(number-1)*15)
    srt = compiled.srt
    short, _ = GeneratedShort.objects.update_or_create(job=job, short_number=number, defaults=dict(
        start_time=candidate['start'], end_time=candidate['end'], duration=duration,
        title=title, description=description, hashtags=hashtags, transcript=text,
        caption_file=relative(srt) if srt else '', video_file=relative(output), thumbnail_file=relative(thumbnail),
        quality_score=report['score'], quality_report=report, rights_review_status='manual_review_required',
        timeline=compiled.timeline.model_dump(mode='json'), qa_status=report.get('qa_status') or '',
        project_id=job.project_id, kind='highlight'))
    short.segments.all().delete()
    TranscriptSegment.objects.bulk_create([TranscriptSegment(short=short,
        start_ms=round(c['start']*1000), end_ms=round(c['end']*1000), original_text=c['text'],
        translated_english=c.get('translated_english',''), translated_hindi=c.get('translated_hindi',''),
        detected_language=c.get('detected_language') or 'und', confidence=c.get('confidence')) for c in compiled.cues])
