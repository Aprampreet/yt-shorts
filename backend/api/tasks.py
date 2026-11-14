import logging
import os
import shutil
import time
from pathlib import Path
from celery import shared_task
from billiard.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.utils import timezone
from .models import GenerationJob, GeneratedShort, TranscriptSegment, ScheduledPost
from .services.media import StudioError, download, probe, disk_check, run
from .services.analysis import sample_signals, candidates, select_distinct, configured_weights
from .services.captions import local_cues, write_captions
from .services.local_transcription import transcribe_source
from .services.languages import label_cues, caption_text
from .services.providers import HuggingFaceProvider
from .services.groq import enabled as groq_enabled, finalize_description, generate_metadata, transcribe_clip
from .services.rendering import render_command, validate_output

log = logging.getLogger(__name__)
TERMINAL = {'completed', 'failed', 'cancelled', 'needs_review'}


class Cancelled(Exception):
    pass


def update(job, status, stage, progress):
    if not GenerationJob.objects.filter(pk=job.pk).exclude(status='cancelled').update(
            status=status, stage=stage, progress=progress):
        raise Cancelled()
    job.status, job.stage, job.progress = status, stage, progress


def relative(path):
    return str(path.relative_to(settings.MEDIA_ROOT))


@shared_task
def generate_job(job_id):
    # Atomic claim: duplicate deliveries cannot run or overwrite a job concurrently.
    claimed = GenerationJob.objects.filter(pk=job_id, status='queued').update(
        status='downloading', started_at=timezone.now(), error_code='', error_message='')
    if not claimed:
        return
    job = GenerationJob.objects.select_related('source').get(pk=job_id)
    root = settings.MEDIA_ROOT / 'jobs' / str(job.id)
    folders = {name: root/name for name in ['source', 'audio', 'captions', 'renders', 'thumbnails', 'reports']}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timings, warnings = {}, []
    provider = None
    def mark(name, before):
        timings[name] = round(time.monotonic()-before, 2)
        if time.monotonic()-started > settings.JOB_TIMEOUT:
            raise StudioError('job_timeout', 'Job exceeded its processing time budget.')
        disk_check(settings.MEDIA_ROOT)
    try:
        provider = HuggingFaceProvider()
        disk_check(settings.MEDIA_ROOT)
        update(job, 'downloading', 'Retrieving authorized source', 4)
        tick = time.monotonic()
        if job.source.source_type == 'upload':
            source = folders['source']/'upload.mp4'
            if not source.exists():
                raise StudioError('source_expired', 'Source upload expired. Upload the original file again.')
            metadata = job.source.source_metadata
        else:
            retained = [path for path in folders['source'].glob('source.*')
                        if path.suffix.lower() in {'.mp4', '.webm', '.mkv', '.mov'}]
            if len(retained) == 1:
                try:
                    probe(retained[0])
                    source, metadata = retained[0], job.source.source_metadata
                except StudioError:
                    retained[0].unlink(missing_ok=True)
                    source, metadata = download(job.source.source_url, folders['source'])
            else:
                source, metadata = download(job.source.source_url, folders['source'])
        mark('download', tick)
        update(job, 'preprocessing', 'Inspecting timing, scenes and silence', 12)
        tick = time.monotonic()
        info = probe(source)
        s = job.source
        for key in ['duration', 'fps', 'width', 'height']:
            setattr(s, key, info[key])
        s.title = metadata.get('title') or 'Uploaded source'
        s.source_metadata = metadata
        s.save()
        signals = sample_signals(source, info['duration'], info['has_audio'])
        mark('preprocessing', tick)
        update(job, 'transcribing', 'Transcribing source audio locally', 25)
        tick = time.monotonic()
        cues = []
        if info['has_audio']:
            try:
                cues = transcribe_source(source, folders['audio'], info['duration'])
                if cues:
                    warnings.append('Locally generated speech captions need human accuracy review before publishing.')
            except StudioError as exc:
                warnings.append(exc.message)
        if not cues and info['has_audio'] and os.getenv('HF_TOKEN') and os.getenv('HF_TRANSCRIPTION_MODEL'):
            try:
                # Bounded 60-second mono chunks. Approximate timing is explicitly reported.
                for offset in range(0, int(info['duration'])+1, 60):
                    if offset >= info['duration']:
                        break
                    update(job, 'transcribing', f'Transcribing audio at {offset}s', 25)
                    chunk_duration = min(60, info['duration']-offset)
                    audio = folders['audio']/'chunk.flac'
                    run(['ffmpeg', '-nostdin', '-y', '-ss', offset, '-i', source, '-t', chunk_duration,
                         '-vn', '-ac', '1', '-ar', '16000', audio])
                    text, chunks = provider.transcribe(audio)
                    if not chunks and text.strip():
                        chunks = [dict(start=0, end=chunk_duration, text=text, timing='approximate_chunk')]
                    for cue in chunks:
                        a, b = max(0, cue['start']), min(chunk_duration, cue['end'])
                        if b > a:
                            cues.append(dict(cue, start=a+offset, end=b+offset))
            except StudioError as exc:
                warnings.append(exc.message)
        cues = label_cues(cues)
        if not cues:
            if info['has_audio'] and job.settings.get('require_captions', True):
                raise StudioError('transcription_unavailable',
                    'No usable speech transcript was generated from the source audio. Check the local Whisper model and worker log, then retry.')
            warnings.append('No speech transcript available; this clip will have no spoken captions. Selection is heuristic.')
        mark('transcription', tick)
        update(job, 'analyzing', 'Scoring candidate moments', 36)
        target = job.settings.get('target_duration', 30)
        weights = configured_weights()
        pool = candidates(info['duration'], target, cues, signals, weights)
        if cues and os.getenv('HF_TEXT_MODEL') and os.getenv('HF_TOKEN'):
            for candidate in sorted(pool, key=lambda c: c['score'], reverse=True)[:6]:
                update(job, 'analyzing', 'Reviewing candidate stories with hosted AI', 38)
                if not candidate['transcript']:
                    continue
                try:
                    editorial = provider.analyze(candidate['transcript'])
                    candidate['editorial'] = editorial
                    for key in ['hook', 'coherence', 'clarity', 'payoff']:
                        candidate['components'][key] = editorial[key]
                    candidate['score'] = round(sum(candidate['components'][k]*w for k,w in weights.items())*100,1)
                    candidate['reason'] = editorial['reason']
                except StudioError as exc:
                    warnings.append(exc.message)
                    break
        update(job, 'selecting', 'Choosing distinct highlights', 42)
        selected = select_distinct(pool)
        if len(selected) < 3:
            warnings.append(f'Only {len(selected)} non-overlapping highlights fit the source and target duration. No duplicate footage was added.')
        job.analysis = dict(signals=signals, candidates=pool, weights=weights, warnings=warnings,
                            transcription_timing='Phrase subdivisions are approximate; source/provider timestamps retained when available.')
        GenerationJob.objects.filter(pk=job.pk).update(analysis=job.analysis)
        tick = time.monotonic()
        for number, candidate in enumerate(selected, 1):
            update(job, 'editing', f'Preparing Short {number} of {len(selected)}', 44+(number-1)*15)
            clip_cues = local_cues(cues, candidate['start'], candidate['end'])
            caption_timing = job.analysis['transcription_timing']
            if info['has_audio'] and groq_enabled():
                improved = transcribe_clip(source, folders['audio'], candidate['start'],
                                           candidate['end']-candidate['start'], number)
                if improved:
                    clip_cues = label_cues(improved)
                    caption_timing = ('Groq word timestamps used for caption phrases.' if
                                      improved[0]['timing'] == 'groq_word' else
                                      'Groq segment timestamps used; phrase subdivisions are approximate.')
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
                srt, ass = write_captions(clip_cues, folders['captions'], number, job.settings.get('caption_style', 'minimal'), subtitle_mode)
            if commentary and not ass:
                srt, ass = write_captions([], folders['captions'], number, job.settings.get('caption_style', 'minimal'), subtitle_mode)
                from .services.captions import stamp
                srt.write_text(f'1\n{stamp(0)} --> {stamp(min(5,candidate["end"]-candidate["start"]))}\nANALYSIS: {commentary["on_screen_text"]}\n')
            if commentary and ass:
                from .services.captions import stamp
                overlay = commentary['on_screen_text'].replace('\\','').replace('{','').replace('}','').replace('\n',' ')
                with ass.open('a') as subtitle:
                    subtitle.write(f'\nDialogue: 1,{stamp(0,True)},{stamp(min(5,candidate["end"]-candidate["start"]),True)},Main,,0,0,0,,{{\\pos(540,330)\\fad(180,180)}}ANALYSIS: {overlay}\n')
            duration = candidate['end']-candidate['start']
            output = folders['renders']/f'{number}.mp4'
            partial = folders['renders']/f'{number}.partial.mp4'
            command, dimensions = render_command(source.resolve(), partial.resolve(), candidate['start'], duration, info,
                job.settings.get('resolution', '720'), job.settings.get('layout', 'preserve'), ass)
            update(job, 'rendering', f'Rendering Short {number} of {len(selected)}', 48+(number-1)*15)
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
            title = editorial['title_candidates'][0][:500] if editorial else (text[:85] or f'{s.title[:75]} · Highlight {number}')
            groq_metadata = generate_metadata(text)
            if groq_metadata:
                title = groq_metadata['title']
            hashtags = groq_metadata['hashtags'] if groq_metadata else editorial['hashtags'] if editorial else ['#Shorts']
            base_description = groq_metadata['description'] if groq_metadata else editorial['description'] if editorial else (
                'A standout moment from this Short: ' + (text[:220].rstrip() or 'watch the moment unfold.'))
            description = finalize_description(base_description, hashtags)
            report['rights_review'] = {
                'source_url': s.source_url, 'original_creator': metadata.get('uploader') or metadata.get('channel') or 'Not independently verified',
                'rights_declaration': s.rights_declaration, 'permission_reference': s.permission_reference,
                'permission_verification': 'User declaration only; not independently verified',
                'attribution_requirements': 'Check the source license or permission terms',
                'music_rights': 'Unknown; original source audio retained' if info['has_audio'] else 'No source audio',
                'third_party_footage': 'Not automatically determined',
                'originality': 'Grounded on-screen analysis based on quoted source speech' if commentary else 'Source-based highlight with captions and reframing; no original commentary',
                'copyright_permission': 'Requires review', 'reused_content_eligibility': 'Requires independent YouTube policy review'}
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
        mark('rendering_and_qa', tick)
        job.analysis.update(warnings=warnings, timings=timings, inference_calls=provider.calls,
            total_seconds=round(time.monotonic()-started,2), disk_bytes=disk_check(root))
        # Human rights review is always needed; a declaration is not proof of ownership.
        update(job, 'needs_review', 'Ready for final review', 100)
        GenerationJob.objects.filter(pk=job.pk).update(analysis=job.analysis, completed_at=timezone.now())
    except Cancelled:
        log.info('Job cancelled: %s', job.id)
    except (StudioError, SoftTimeLimitExceeded) as exc:
        code = getattr(exc, 'code', 'job_timeout')
        message = getattr(exc, 'message', 'Processing exceeded its time limit. Try a shorter source.')
        GenerationJob.objects.filter(pk=job.pk).exclude(status='cancelled').update(
            status='failed', stage='Action needed', error_code=code, error_message=message, completed_at=timezone.now())
    except Exception:
        log.exception('Job failed: %s', job.id)
        GenerationJob.objects.filter(pk=job.pk).exclude(status='cancelled').update(status='failed', stage='Action needed',
            error_code='internal_error', error_message='Processing failed. Check worker logs and retry.', completed_at=timezone.now())
    finally:
        shutil.rmtree(folders['audio'], ignore_errors=True)
        for partial in folders['renders'].glob('*.partial.mp4'):
            partial.unlink(missing_ok=True)


@shared_task
def rerender_short(short_id):
    short = GeneratedShort.objects.select_related('job__source').get(pk=short_id)
    root = settings.MEDIA_ROOT/'jobs'/str(short.job_id)
    source_folder = root/'source'
    source_files = [p for p in source_folder.iterdir() if p.suffix.lower() in {'.mp4','.mkv','.webm','.mov'}] if source_folder.exists() else []
    if len(source_files) != 1:
        short.quality_report = dict(short.quality_report, transcript_edit_status='source_expired')
        short.save(update_fields=['quality_report'])
        return
    folder = root/'captions'
    cues = [dict(start=s.start_ms/1000,end=s.end_ms/1000,text=s.original_text,
        translated_english=s.translated_english,translated_hindi=s.translated_hindi,
        detected_language=s.detected_language,confidence=s.confidence) for s in short.segments.all()]
    try:
        version = f'edited-{short.id.hex}'
        srt, ass = write_captions(cues, folder, version, short.job.settings.get('caption_style','minimal'),
                                   short.job.settings.get('subtitle_mode','original'))
        if not srt.read_text().strip():
            raise StudioError('no_captions','The selected subtitle mode has no available text. Edit translations first.')
        output = root/'renders'/f'{short.short_number}.edited.partial.mp4'
        final = settings.MEDIA_ROOT/short.video_file
        info = probe(source_files[0])
        command, dims = render_command(source_files[0].resolve(),output.resolve(),short.start_time,short.duration,info,
            short.job.settings.get('resolution','720'),short.job.settings.get('layout','preserve'),ass)
        run(command,cwd=folder)
        report = validate_output(output,short.duration,dims,info['has_audio'])
        if not all(report['checks'].values()):
            raise StudioError('output_invalid','Edited subtitles failed render validation.')
        output.replace(final)
        srt.replace(settings.MEDIA_ROOT/short.caption_file)
        ass.replace((settings.MEDIA_ROOT/short.caption_file).with_suffix('.ass'))
        short.transcript = ' '.join(s.original_text for s in short.segments.all())
        short.quality_report = dict(short.quality_report, transcript_edit_status='ready',
                                    edited_captions_revalidated=report['checks'])
        short.save(update_fields=['transcript','quality_report'])
    except (StudioError, Exception) as exc:
        log.exception('Transcript re-render failed for %s',short.id)
        short.quality_report = dict(short.quality_report,transcript_edit_status='failed',
                                    transcript_edit_error=getattr(exc,'message','Could not render edited captions. Check worker logs.'))
        short.save(update_fields=['quality_report'])
    finally:
        for path in folder.glob(f'edited-{short.id.hex}.*'):
            path.unlink(missing_ok=True)
        (root/'renders'/f'{short.short_number}.edited.partial.mp4').unlink(missing_ok=True)


@shared_task
def publish_youtube_post(post_id):
    from .services.social import youtube_upload
    if not ScheduledPost.objects.filter(pk=post_id,status='queued',approved_at__isnull=False,
        external_post_id='').update(status='uploading'):
        return
    post = ScheduledPost.objects.select_related('generated_short__job__source','social_account').get(pk=post_id)
    try:
        if post.platform != 'youtube' or not post.social_account or post.social_account.connection_status != 'connected':
            raise StudioError('account_unavailable','A connected YouTube account is required.')
        if not post.generated_short or not (
            post.generated_short.rights_review_status == 'rights_documented' or
            post.approval_basis == 'source_declaration' and post.generated_short.job.source.rights_declaration
        ):
            raise StudioError('rights_unresolved','The source rights declaration is unavailable.')
        video = settings.MEDIA_ROOT/post.generated_short.video_file
        if not video.is_file():
            raise StudioError('video_expired','The approved video expired; render it again before uploading.')
        external_id, privacy = youtube_upload(post,video)
        post.external_post_id = external_id
        post.published_url = 'https://www.youtube.com/watch?v='+external_id
        post.status = 'scheduled' if post.scheduled_at else 'published' if privacy == 'public' else 'uploaded_private'
        post.published_at = timezone.now() if post.status == 'published' else None
        post.last_error = 'YouTube kept this upload private; review the channel and API project in YouTube Studio.' if post.status == 'uploaded_private' else ''
        post.save(update_fields=['external_post_id','published_url','status','published_at','last_error'])
    except Exception as exc:
        log.exception('YouTube upload failed for post %s',post_id)
        post.retry_count += 1
        import httpx
        status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
        transient = isinstance(exc, httpx.TransportError) or status_code in {429, 500, 502, 503, 504}
        if transient and post.retry_count < 3:
            post.status = 'queued'
            post.last_error = 'Temporary YouTube upload error. Retrying automatically.'
            post.save(update_fields=['retry_count','status','last_error'])
            try:
                publish_youtube_post.apply_async(args=[post_id], countdown=30 * 2 ** (post.retry_count-1))
                return
            except Exception:
                log.exception('Could not enqueue YouTube retry for post %s', post_id)
        post.status = 'failed'
        post.last_error = getattr(exc,'message','YouTube upload failed. Review account, quota and worker logs.')
        post.save(update_fields=['retry_count','status','last_error'])


@shared_task
def refresh_youtube_publications():
    import httpx
    from .services.social import YOUTUBE_API, current_token
    due = ScheduledPost.objects.filter(platform='youtube',status='scheduled',scheduled_at__lte=timezone.now()).select_related('social_account')[:50]
    for post in due:
        try:
            token = current_token(post.social_account)
            response = httpx.get(YOUTUBE_API+'/videos',params={'part':'status','id':post.external_post_id},
                headers={'Authorization':'Bearer '+token},timeout=20)
            response.raise_for_status()
            items = response.json().get('items',[])
            if items and items[0].get('status',{}).get('privacyStatus') == 'public':
                ScheduledPost.objects.filter(pk=post.pk,status='scheduled').update(status='published',published_at=timezone.now(),last_error='')
        except Exception:
            log.exception('Could not confirm YouTube publication for %s',post.pk)
