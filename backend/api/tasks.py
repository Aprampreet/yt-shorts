import logging
import shutil
from billiard.exceptions import SoftTimeLimitExceeded
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from .models import GenerationJob, GeneratedShort, ScheduledPost
from .pipeline import highlights, ingest, shorts, transcribe
from .pipeline.context import Cancelled, JobContext, relative, update  # noqa: F401  (re-exported)
from .services.media import StudioError, probe, disk_check, run
from .services.captions import write_captions
from .services.providers import HuggingFaceProvider
from .services.rendering import render_command, validate_output

log = logging.getLogger(__name__)
TERMINAL = {'completed', 'failed', 'cancelled', 'needs_review'}


@shared_task
def generate_job(job_id):
    # Atomic claim: duplicate deliveries cannot run or overwrite a job concurrently.
    claimed = GenerationJob.objects.filter(pk=job_id, status='queued').update(
        status='downloading', started_at=timezone.now(), error_code='', error_message='')
    if not claimed:
        return
    job = GenerationJob.objects.select_related('source').get(pk=job_id)
    ctx = JobContext.create(job)
    try:
        ctx.provider = HuggingFaceProvider()
        disk_check(settings.MEDIA_ROOT)
        ingest.acquire_source(ctx)
        ingest.inspect_source(ctx)
        transcribe.transcribe(ctx)
        highlights.score_and_select(ctx)
        shorts.render_shorts(ctx)
        shorts.finish_job(ctx, ctx.provider.calls)
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
        shutil.rmtree(ctx.folders['audio'], ignore_errors=True)
        for partial in ctx.folders['renders'].glob('*.partial.mp4'):
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
