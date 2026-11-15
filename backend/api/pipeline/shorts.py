"""Per-clip Shorts stages: transcript refinement -> captions -> render -> QA -> metadata -> persist."""
import os
import time

from django.utils import timezone

from ..models import GeneratedShort, GenerationJob, TranscriptSegment
from ..services.captions import local_cues, stamp, write_captions
from ..services.groq import enabled as groq_enabled, transcribe_clip
from ..services.languages import caption_text, label_cues
from ..services.media import StudioError, disk_check, run
from ..services.rendering import render_command, validate_output
from .context import relative, update
from .metadata import clip_metadata
from .rights import build_rights_review


def render_shorts(ctx):
    selected = ctx.selected
    tick = time.monotonic()
    for number, candidate in enumerate(selected, 1):
        make_short(ctx, number, candidate, len(selected))
    ctx.mark('rendering_and_qa', tick)


def finish_job(ctx, provider_calls):
    job = ctx.job
    job.analysis.update(warnings=ctx.warnings, timings=ctx.timings, inference_calls=provider_calls,
        total_seconds=round(time.monotonic()-ctx.started, 2), disk_bytes=disk_check(ctx.root))
    # Human rights review is always needed; a declaration is not proof of ownership.
    update(job, 'needs_review', 'Ready for final review', 100)
    GenerationJob.objects.filter(pk=job.pk).update(analysis=job.analysis, completed_at=timezone.now())


def clip_transcript(ctx, number, candidate):
    job, info = ctx.job, ctx.info
    clip_cues = local_cues(ctx.cues, candidate['start'], candidate['end'])
    caption_timing = job.analysis['transcription_timing']
    if info['has_audio'] and groq_enabled():
        improved = transcribe_clip(ctx.source, ctx.folders['audio'], candidate['start'],
                                   candidate['end']-candidate['start'], number)
        if improved:
            clip_cues = label_cues(improved)
            caption_timing = ('Groq word timestamps used for caption phrases.' if
                              improved[0]['timing'] == 'groq_word' else
                              'Groq segment timestamps used; phrase subdivisions are approximate.')
    return clip_cues, caption_timing


def build_captions(ctx, number, candidate, clip_cues, commentary):
    """Translate/filter cues and write SRT+ASS. Returns (clip_cues, srt, ass)."""
    job, warnings, provider = ctx.job, ctx.warnings, ctx.provider
    srt = ass = None
    subtitle_mode = job.settings.get('subtitle_mode', 'original')
    if clip_cues and subtitle_mode != 'original' and os.getenv('HF_TRANSLATION_MODEL') and os.getenv('HF_TOKEN'):
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
    if clip_cues:
        filters = run(['ffmpeg', '-hide_banner', '-filters'])[0]
        if ' ass ' not in filters:
            raise StudioError('libass_required', 'Animated captions need an FFmpeg build with libass. Install ffmpeg-full on macOS or set FFMPEG_BIN to a compatible build, then retry.')
        srt, ass = write_captions(clip_cues, ctx.folders['captions'], number, job.settings.get('caption_style', 'minimal'), subtitle_mode)
    if commentary and not ass:
        srt, ass = write_captions([], ctx.folders['captions'], number, job.settings.get('caption_style', 'minimal'), subtitle_mode)
        srt.write_text(f'1\n{stamp(0)} --> {stamp(min(5,candidate["end"]-candidate["start"]))}\nANALYSIS: {commentary["on_screen_text"]}\n')
    if commentary and ass:
        overlay = commentary['on_screen_text'].replace('\\','').replace('{','').replace('}','').replace('\n',' ')
        with ass.open('a') as subtitle:
            subtitle.write(f'\nDialogue: 1,{stamp(0,True)},{stamp(min(5,candidate["end"]-candidate["start"]),True)},Main,,0,0,0,,{{\\pos(540,330)\\fad(180,180)}}ANALYSIS: {overlay}\n')
    return clip_cues, srt, ass


def render_clip(ctx, number, candidate, total, ass):
    """Render with one repair retry, validate, promote the partial and cut a thumbnail."""
    job, info, folders, source = ctx.job, ctx.info, ctx.folders, ctx.source
    duration = candidate['end']-candidate['start']
    output = folders['renders']/f'{number}.mp4'
    partial = folders['renders']/f'{number}.partial.mp4'
    command, dimensions = render_command(source.resolve(), partial.resolve(), candidate['start'], duration, info,
        job.settings.get('resolution', '720'), job.settings.get('layout', 'preserve'), ass)
    update(job, 'rendering', f'Rendering Short {number} of {total}', 48+(number-1)*15)
    for attempt in range(2):
        run(command, cwd=folders['captions'])
        update(job, 'validating', f'Quality checking Short {number}', 55+(number-1)*15)
        report = validate_output(partial, duration, dimensions, info['has_audio'])
        if all(report['checks'].values()):
            break
        if attempt:
            raise StudioError('output_invalid', 'Rendered output failed technical validation after one repair attempt.')
    partial.replace(output)
    thumbnail = folders['thumbnails']/f'{number}.jpg'
    run(['ffmpeg', '-nostdin', '-y', '-ss', min(1,duration/2), '-i', output, '-frames:v', '1', '-vf', 'scale=270:480', thumbnail])
    return output, thumbnail, report, duration


def make_short(ctx, number, candidate, total):
    job, info, provider, warnings = ctx.job, ctx.info, ctx.provider, ctx.warnings
    s = job.source
    update(job, 'editing', f'Preparing Short {number} of {total}', 44+(number-1)*15)
    clip_cues, caption_timing = clip_transcript(ctx, number, candidate)
    text = ' '.join(c['text'] for c in clip_cues)
    editorial = candidate.get('editorial')
    if not editorial and text and os.getenv('HF_TEXT_MODEL') and os.getenv('HF_TOKEN'):
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
    clip_cues, srt, ass = build_captions(ctx, number, candidate, clip_cues, commentary)
    output, thumbnail, report, duration = render_clip(ctx, number, candidate, total, ass)
    title, description, hashtags, groq_metadata = clip_metadata(text, editorial, s.title, number)
    report['rights_review'] = build_rights_review(s, ctx.metadata, info, commentary)
    report.update(highlight_score=candidate['score'], components=candidate['components'],
        decision=editorial['reason'] if editorial else candidate['reason'],
        transformations=['Precise trim', '9:16 '+job.settings.get('layout','preserve')+' layout',
            'Restrained color adjustment', 'Audio normalization and peak limiting' if info['has_audio'] else 'No source audio',
            'Animated phrase captions (approximate subdivisions)' if ass else 'No captions available'],
        title_candidates=editorial['title_candidates'] if editorial else [title],
        metadata_source='groq' if groq_metadata else 'existing',
        file_size=output.stat().st_size, caption_timing=caption_timing,
        original_commentary=commentary)
    update(job, 'validating', f'Saving validated Short {number}', 56+(number-1)*15)
    short, _ = GeneratedShort.objects.update_or_create(job=job, short_number=number, defaults=dict(
        start_time=candidate['start'], end_time=candidate['end'], duration=duration,
        title=title, description=description, hashtags=hashtags, transcript=text,
        caption_file=relative(srt) if srt else '', video_file=relative(output), thumbnail_file=relative(thumbnail),
        quality_score=report['score'], quality_report=report, rights_review_status='manual_review_required'))
    short.segments.all().delete()
    TranscriptSegment.objects.bulk_create([TranscriptSegment(short=short,
        start_ms=round(c['start']*1000), end_ms=round(c['end']*1000), original_text=c['text'],
        translated_english=c.get('translated_english',''), translated_hindi=c.get('translated_hindi',''),
        detected_language=c.get('detected_language') or 'und', confidence=c.get('confidence')) for c in clip_cues])
