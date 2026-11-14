import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import uuid
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo
from django.http import HttpResponseRedirect
import zipfile
from pathlib import Path
from typing import Literal
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import NinjaAPI, Schema, File, Form
from ninja.files import UploadedFile
from ninja.errors import HttpError
from ninja.security import APIKeyCookie
from pydantic import Field
from .models import SourceVideo, GenerationJob, GeneratedShort, TranscriptSegment, ConnectedSocialAccount, ScheduledPost, PostingPreference
from .services.media import StudioError, validate_youtube, disk_check
from .tasks import generate_job, rerender_short, TERMINAL

log = logging.getLogger(__name__)


class StudioSession(APIKeyCookie):
    param_name = settings.SESSION_COOKIE_NAME
    def authenticate(self, request, key):
        return request.session.get('studio_owner')


api = NinjaAPI(title='Shorts Studio', version='1.0.0', auth=StudioSession())


class JobOptions(Schema):
    caption_style: Literal['gaming', 'minimal', 'cinematic', 'dynamic'] = 'minimal'
    subtitle_mode: Literal['original', 'english', 'hindi', 'bilingual'] = 'original'
    require_captions: bool = True
    target_duration: int = Field(default=30, ge=20, le=60)
    resolution: Literal['720', '1080'] = '720'
    layout: Literal['preserve', 'center'] = 'preserve'
    mode: Literal['source_highlight','original_analysis'] = 'source_highlight'


class JobInput(Schema):
    url: str = Field(max_length=2048)
    rights_declaration: bool
    permission_reference: str = Field(default='', max_length=2000)
    options: JobOptions = Field(default_factory=JobOptions)


class Error(Schema):
    error: str
    message: str


@api.exception_handler(StudioError)
def studio_error(request, exc):
    return api.create_response(request, {'error': exc.code, 'message': exc.message}, status=400)


@api.exception_handler(Exception)
def unexpected_error(request, exc):
    log.exception('Unhandled API error', exc_info=exc)
    return api.create_response(request, {'error':'internal_error',
        'message':'The studio could not complete this request. Check backend logs.'}, status=500)


def owned_job(request, job_id):
    return get_object_or_404(GenerationJob.objects.select_related('source'), pk=job_id, owner=request.auth)


def owned_short(request, short_id):
    return get_object_or_404(GeneratedShort.objects.select_related('job__source'), pk=short_id, job__owner=request.auth)


def short_data(short):
    base = f'/api/v1/shorts/{short.pk}'
    return dict(id=str(short.id), short_number=short.short_number, start_time=short.start_time,
        end_time=short.end_time, duration=short.duration, title=short.title, description=short.description,
        hashtags=short.hashtags, transcript=short.transcript, quality_score=short.quality_score,
        quality_report=short.quality_report, rights_review_status=short.rights_review_status,
        segments=[dict(id=str(seg.id),start_ms=seg.start_ms,end_ms=seg.end_ms,original_text=seg.original_text,
            translated_english=seg.translated_english,translated_hindi=seg.translated_hindi,
            detected_language=seg.detected_language,confidence=seg.confidence) for seg in short.segments.all()],
        video_url=base+'/video', download_url=base+'/download', thumbnail_url=base+'/thumbnail',
        caption_url=base+'/captions' if short.caption_file else None)


def job_data(job):
    return dict(id=str(job.id), status=job.status, stage=job.stage, progress=job.progress,
        error_code=job.error_code, error_message=job.error_message, settings=job.settings,
        analysis={k:v for k,v in job.analysis.items() if k not in {'signals','candidates'}}, created_at=job.created_at.isoformat(), title=job.source.title or 'New generation',
        source_url=job.source.source_url, permission_reference=job.source.permission_reference,
        rights_declaration=job.source.rights_declaration, license_status=job.source.license_status,
        results=[short_data(s) for s in job.shorts.all()])


@api.get('/v1/session', auth=None)
def session(request):
    if 'studio_owner' not in request.session:
        request.session['studio_owner'] = str(uuid.uuid4())
    return {'csrf_token': get_token(request)}


@api.get('/v1/capabilities', auth=None)
def capabilities(request):
    approved = os.getenv('AI_ALLOW_BILLABLE','false').lower() == 'true'
    token = bool(os.getenv('HF_TOKEN'))
    local_model = Path(os.getenv('LOCAL_WHISPER_MODEL') or settings.BASE_DIR/'.local/models/ggml-base-q5_1.bin')
    local_asr = bool((os.getenv('LOCAL_WHISPER_BIN') or shutil.which('whisper-cli')) and local_model.is_file())
    return {'hosted_inference_enabled':approved and token,
        'asr':local_asr or (approved and token and bool(os.getenv('HF_TRANSCRIPTION_MODEL'))),
        'translation':approved and token and bool(os.getenv('HF_TRANSLATION_MODEL')),
        'original_analysis':approved and token and bool(os.getenv('HF_TEXT_MODEL')),
        'youtube_oauth_configured':bool(os.getenv('YOUTUBE_CLIENT_ID') and os.getenv('YOUTUBE_CLIENT_SECRET') and os.getenv('SOCIAL_ENCRYPTION_KEY')),
        'instagram_publishing_available':False,
        'reason':('Local audio transcription is available; hosted analysis and translation require explicit billing approval.'
                  if local_asr and not approved else
                  'Install whisper-cli and a multilingual model for offline captions.' if not local_asr and not approved else '')}


@api.get('/v1/health', auth=None)
def health(request):
    local_model = Path(os.getenv('LOCAL_WHISPER_MODEL') or settings.BASE_DIR/'.local/models/ggml-base-q5_1.bin')
    return {'status': 'ok', 'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
            'transcription_configured': bool(((os.getenv('LOCAL_WHISPER_BIN') or shutil.which('whisper-cli')) and local_model.is_file()) or
                                             (os.getenv('AI_ALLOW_BILLABLE','false').lower() == 'true' and os.getenv('HF_TOKEN') and os.getenv('HF_TRANSCRIPTION_MODEL')))}


def capacity(owner):
    disk_check(settings.MEDIA_ROOT)
    active = GenerationJob.objects.exclude(status__in=TERMINAL).count()
    if active >= 10 or GenerationJob.objects.filter(owner=owner).exclude(status__in=TERMINAL).count() >= 1:
        raise HttpError(429, 'A generation is already active or the queue is full. Please wait.')
    if GenerationJob.objects.filter(owner=owner, created_at__gte=timezone.now()-timedelta(hours=1)).count() >= 6:
        raise HttpError(429, 'Hourly generation limit reached. Try again later.')


def enqueue(job):
    try:
        generate_job.delay(str(job.pk))
    except Exception:
        log.exception('Could not enqueue job %s', job.pk)
        job.status = 'failed'
        job.stage = 'Worker unavailable'
        job.error_code = 'queue_unavailable'
        job.error_message = 'Could not reach Redis. Start Redis and the Celery worker, then retry.'
        job.save(update_fields=['status','stage','error_code','error_message'])


@api.post('/v1/jobs', response={202: dict})
@api.post('/generate-short', response={202: dict}, deprecated=True)
def create_job(request, data: JobInput):
    if data.options.mode == 'original_analysis' and not (os.getenv('AI_ALLOW_BILLABLE','false').lower() == 'true' and os.getenv('HF_TOKEN') and os.getenv('HF_TEXT_MODEL')):
        raise StudioError('commentary_unavailable','Original analysis requires an explicitly approved hosted text provider. Source Highlight remains available without it.')
    if not data.rights_declaration:
        raise StudioError('rights_required', 'Confirm ownership or permission, including commercial-use rights, before processing.')
    url = validate_youtube(data.url)
    capacity(request.auth)
    with transaction.atomic():
        source = SourceVideo.objects.create(source_url=url, rights_declaration=True,
            permission_reference=data.permission_reference, license_status='verification_needed')
        job = GenerationJob.objects.create(source=source, owner=request.auth, settings=data.options.model_dump())
    enqueue(job)
    return 202, job_data(job)


@api.post('/v1/uploads', response={202: dict})
def upload_job(request, file: File[UploadedFile], rights_declaration: Form[bool],
               permission_reference: Form[str] = '', options: Form[str] = '{}'):
    if not rights_declaration:
        raise StudioError('rights_required', 'Confirm ownership or permission before uploading.')
    if file.size > settings.MAX_SOURCE_BYTES:
        raise HttpError(413, 'Upload exceeds file-size limit.')
    if len(permission_reference) > 2000:
        raise HttpError(422, 'Permission reference is too long.')
    try:
        parsed = JobOptions.model_validate_json(options)
    except ValueError:
        raise HttpError(422, 'Invalid generation options.')
    if parsed.mode == 'original_analysis' and not (os.getenv('AI_ALLOW_BILLABLE','false').lower() == 'true' and os.getenv('HF_TOKEN') and os.getenv('HF_TEXT_MODEL')):
        raise StudioError('commentary_unavailable','Original analysis requires an explicitly approved hosted text provider.')
    capacity(request.auth)
    with transaction.atomic():
        source = SourceVideo.objects.create(source_type='upload', title=Path(file.name).name[:500],
            rights_declaration=True, permission_reference=permission_reference,
            source_metadata={'title': Path(file.name).name[:500]})
        job = GenerationJob.objects.create(source=source, owner=request.auth, settings=parsed.model_dump())
    folder = settings.MEDIA_ROOT/'jobs'/str(job.pk)/'source'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with (folder/'upload.mp4').open('wb') as output:
            size = 0
            for chunk in file.chunks():
                size += len(chunk)
                if size > settings.MAX_SOURCE_BYTES:
                    raise StudioError('source_limit', 'Upload exceeds file-size limit.')
                output.write(chunk)
    except Exception:
        shutil.rmtree(folder.parent, ignore_errors=True)
        job.delete()
        source.delete()
        raise
    enqueue(job)
    return 202, job_data(job)


@api.get('/v1/jobs')
def history(request):
    return [job_data(j) for j in GenerationJob.objects.filter(owner=request.auth).select_related('source').prefetch_related('shorts')[:30]]


@api.get('/v1/jobs/{job_id}')
@api.get('/v1/jobs/{job_id}/results')
def job_status(request, job_id: uuid.UUID):
    return job_data(owned_job(request, job_id))


@api.get('/v1/jobs/{job_id}/analysis')
def analysis_report(request, job_id: uuid.UUID):
    return owned_job(request, job_id).analysis


@api.post('/v1/jobs/{job_id}/cancel')
def cancel(request, job_id: uuid.UUID):
    job = owned_job(request, job_id)
    GenerationJob.objects.filter(pk=job.pk).exclude(status__in=TERMINAL).update(
        status='cancelled', stage='Cancelled', completed_at=timezone.now())
    job.refresh_from_db()
    return job_data(job)


@api.post('/v1/jobs/{job_id}/retry', response={202: dict})
def retry(request, job_id: uuid.UUID):
    job = owned_job(request, job_id)
    if job.status != 'failed':
        raise HttpError(409, 'Only failed jobs can be retried. Use regenerate for finished jobs.')
    capacity(request.auth)
    if GenerationJob.objects.filter(pk=job.pk, status='failed').update(status='queued', progress=0,
            stage='Waiting for worker', completed_at=None):
        job.shorts.all().delete()
        job.refresh_from_db()
        enqueue(job)
    return 202, job_data(job)


@api.post('/v1/jobs/{job_id}/regenerate', response={202: dict})
def regenerate(request, job_id: uuid.UUID):
    previous = owned_job(request, job_id)
    if previous.status not in TERMINAL:
        raise HttpError(409, 'Wait for the current job to finish.')
    capacity(request.auth)
    source = previous.source
    if source.source_type == 'upload':
        original = settings.MEDIA_ROOT/'jobs'/str(previous.pk)/'source'/'upload.mp4'
        if not original.exists():
            raise HttpError(410, 'Source expired. Upload it again.')
    job = GenerationJob.objects.create(source=source, owner=request.auth, settings=previous.settings)
    if source.source_type == 'upload':
        folder = settings.MEDIA_ROOT/'jobs'/str(job.pk)/'source'
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, folder/'upload.mp4')
    enqueue(job)
    return 202, job_data(job)


def private_file(relative_path, attachment=False, filename=None, request=None):
    if not relative_path:
        raise Http404('Asset not available')
    root = settings.MEDIA_ROOT.resolve()
    path = (root/relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise Http404('Asset expired or unavailable')
    size = path.stat().st_size
    requested_range = request.headers.get('Range', '') if request and not attachment else ''
    if requested_range:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested_range)
        if not match or not any(match.groups()):
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(int(right) if right else size-1, size-1)
        else:
            start, end = max(0, size-int(right)), size-1
        if start > end or start >= size:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
        def chunks():
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        response = StreamingHttpResponse(chunks(), status=206,
            content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        response['Content-Length'] = end-start+1
    else:
        response = FileResponse(path.open('rb'), as_attachment=attachment, filename=filename or path.name)
    response['Accept-Ranges'] = 'bytes'
    response['Cache-Control'] = 'private, no-store'
    return response


@api.get('/v1/shorts/{short_id}')
@api.get('/v1/shorts/{short_id}/metadata')
def short_metadata(request, short_id: uuid.UUID):
    return short_data(owned_short(request, short_id))


@api.get('/v1/shorts/{short_id}/video')
def video(request, short_id: uuid.UUID):
    return private_file(owned_short(request, short_id).video_file, request=request)


@api.get('/v1/shorts/{short_id}/download')
def download_short(request, short_id: uuid.UUID):
    short = owned_short(request, short_id)
    return private_file(short.video_file, True, f'short-{short.short_number}.mp4')


@api.get('/v1/shorts/{short_id}/thumbnail')
def thumbnail(request, short_id: uuid.UUID):
    return private_file(owned_short(request, short_id).thumbnail_file)


@api.get('/v1/shorts/{short_id}/captions')
def captions(request, short_id: uuid.UUID, format: Literal['srt','ass'] = 'srt'):
    short = owned_short(request, short_id)
    path = str(Path(short.caption_file).with_suffix('.'+format)) if short.caption_file else ''
    return private_file(path, True)


@api.post('/v1/shorts/{short_id}/metadata')
def refresh_metadata(request, short_id: uuid.UUID):
    from .services.providers import HuggingFaceProvider
    from .services.groq import generate_metadata
    short = owned_short(request, short_id)
    if not short.transcript:
        raise StudioError('transcript_required', 'No transcript is available for grounded metadata generation.')
    data = generate_metadata(short.transcript, short.job.source.title,
                             short.job.source.source_metadata.get('description') or '')
    if data:
        short.title = data['title']
        short.description = data['description'] + '\n\nSource: ' + (short.job.source.source_url or short.job.source.title)
        short.hashtags = data['hashtags']
        short.quality_report = dict(short.quality_report, title_candidates=[short.title], metadata_source='groq')
    else:
        data = HuggingFaceProvider().analyze(short.transcript)
        short.title = data['title_candidates'][0][:500]
        short.description = data['description'] + '\n\nSource: ' + (short.job.source.source_url or short.job.source.title)
        short.hashtags = data['hashtags']
        short.quality_report = dict(short.quality_report, title_candidates=data['title_candidates'])
    short.save()
    return short_data(short)


@api.get('/v1/jobs/{job_id}/download')
def download_all(request, job_id: uuid.UUID):
    job = owned_job(request, job_id)
    shorts = list(job.shorts.all())
    if not shorts:
        raise HttpError(409, 'No rendered Shorts available.')
    # Spools to disk instead of holding a potentially large ZIP in memory.
    archive = tempfile.TemporaryFile()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
        for short in shorts:
            for field, suffix in [('video_file', 'mp4'), ('caption_file', 'srt')]:
                value = getattr(short, field)
                if not value:
                    continue
                path = (settings.MEDIA_ROOT/value).resolve()
                if not path.is_relative_to(settings.MEDIA_ROOT.resolve()) or not path.is_file():
                    archive.close()
                    raise HttpError(410, 'An output expired. Regenerate this job.')
                output.write(path, f'short-{short.short_number}.{suffix}')
        output.writestr('metadata.json', json.dumps([short_data(s) for s in shorts], indent=2))
    archive.seek(0)
    return FileResponse(archive, as_attachment=True, filename=f'shorts-{job.pk}.zip')


class SegmentEdit(Schema):
    id: uuid.UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    original_text: str = Field(min_length=1, max_length=1000)
    translated_english: str = Field(default='', max_length=1000)
    translated_hindi: str = Field(default='', max_length=1000)


class TranscriptEdit(Schema):
    segments: list[SegmentEdit] = Field(min_length=1, max_length=500)


@api.put('/v1/shorts/{short_id}/transcript', response={202: dict})
def edit_transcript(request, short_id: uuid.UUID, data: TranscriptEdit):
    short = owned_short(request, short_id)
    source_folder = settings.MEDIA_ROOT/'jobs'/str(short.job_id)/'source'
    if not source_folder.exists():
        raise HttpError(410,'Original source expired; edited captions cannot be burned into the video.')
    existing = list(short.segments.all())
    if len(existing) != len(data.segments) or {s.id for s in existing} != {s.id for s in data.segments}:
        raise HttpError(422,'Edit existing segments only; IDs and segment count must match.')
    sorted_items = sorted(data.segments,key=lambda s:s.start_ms)
    for index, segment in enumerate(sorted_items):
        if segment.end_ms <= segment.start_ms or segment.end_ms > short.duration*1000+100 or (index and segment.start_ms < sorted_items[index-1].end_ms):
            raise HttpError(422,'Segments must be ordered, non-overlapping and within this Short.')
    with transaction.atomic():
        for segment in data.segments:
            TranscriptSegment.objects.filter(short=short,pk=segment.id).update(
                start_ms=segment.start_ms,end_ms=segment.end_ms,original_text=segment.original_text.strip(),
                translated_english=segment.translated_english.strip(),translated_hindi=segment.translated_hindi.strip())
        short.quality_report = dict(short.quality_report,transcript_edit_status='rendering')
        short.save(update_fields=['quality_report'])
    rerender_short.delay(str(short.id))
    return 202, short_data(short)


from .services.social import authorization_url, exchange_code, youtube_identity, seal
from .tasks import publish_youtube_post


def account_data(account):
    return {'id':str(account.id),'platform':account.platform,'account_name':account.account_name,
            'connection_status':account.connection_status,'token_expiry':account.token_expiry.isoformat() if account.token_expiry else None}


def post_data(post):
    return {'id':str(post.id),'short_id':str(post.generated_short_id) if post.generated_short_id else None,'platform':post.platform,
            'account_id':str(post.social_account_id) if post.social_account_id else None,
            'title':post.title,'description':post.description,'hashtags':post.hashtags,
            'scheduled_at':post.scheduled_at.isoformat() if post.scheduled_at else None,
            'timezone':post.timezone,'status':post.status,'last_error':post.last_error,
            'retry_count':post.retry_count,'published_url':post.published_url,
            'approval_basis':post.approval_basis}


@api.get('/v1/social/accounts')
def social_accounts(request):
    return [account_data(a) for a in ConnectedSocialAccount.objects.filter(owner=request.auth,connection_status='connected')]


@api.get('/v1/social/youtube/connect')
def youtube_connect(request):
    state = secrets.token_urlsafe(32)
    request.session['youtube_oauth_state'] = state
    from urllib.parse import urlsplit
    referer = request.headers.get('Referer','')
    parsed = urlsplit(referer)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    request.session['social_return_origin'] = origin if origin in settings.CORS_ALLOWED_ORIGINS else os.getenv('SOCIAL_FRONTEND_URL','http://localhost:5173')
    return {'authorization_url':authorization_url(state)}


@api.get('/v1/social/youtube/callback')
def youtube_callback(request, code: str = '', state: str = '', error: str = ''):
    saved = request.session.pop('youtube_oauth_state',None)
    if error or not code or not saved or not secrets.compare_digest(saved,state):
        raise StudioError('oauth_rejected','YouTube authorization was rejected or expired. Try connecting again.')
    try:
        token = exchange_code(code)
        channel_id,name = youtube_identity(token['access_token'])
    except StudioError:
        raise
    except Exception:
        raise StudioError('oauth_failed','Could not complete YouTube authorization. Verify app credentials and redirect URI.')
    account,_ = ConnectedSocialAccount.objects.update_or_create(owner=request.auth,platform='youtube',
        platform_account_id=channel_id,defaults={'account_name':name,'encrypted_credentials':seal(token),
        'token_expiry':timezone.now()+timedelta(seconds=token.get('expires_in',3600)),'connection_status':'connected'})
    return HttpResponseRedirect(request.session.pop('social_return_origin',os.getenv('SOCIAL_FRONTEND_URL','http://localhost:5173'))+'/?view=schedule&connected=youtube')


@api.post('/v1/social/accounts/{account_id}/disconnect')
def disconnect_social(request, account_id: uuid.UUID):
    account = get_object_or_404(ConnectedSocialAccount,pk=account_id,owner=request.auth)
    ScheduledPost.objects.filter(social_account=account,status__in=['draft','queued','failed']).update(status='cancelled')
    account.encrypted_credentials = ''
    account.connection_status = 'disconnected'
    account.save(update_fields=['encrypted_credentials','connection_status'])
    return {'status':'disconnected'}


class DraftInput(Schema):
    short_id: uuid.UUID
    platform: Literal['youtube','instagram']
    account_id: uuid.UUID | None = None
    title: str = Field(max_length=500)
    description: str = Field(max_length=5000)
    hashtags: list[str] = Field(default_factory=list,max_length=5)
    timezone: str = 'Asia/Kolkata'
    scheduled_at: datetime | None = None


@api.get('/v1/social/posts')
def posts(request):
    return [post_data(p) for p in ScheduledPost.objects.filter(owner=request.auth).select_related('generated_short')[:100]]


@api.post('/v1/social/posts')
def save_draft(request, data: DraftInput):
    short = owned_short(request,data.short_id)
    if data.platform == 'instagram':
        raise StudioError('instagram_unavailable','Instagram connection and publishing require an eligible professional account and public media hosting; this studio has neither configured.')
    account = None
    if data.account_id:
        account = get_object_or_404(ConnectedSocialAccount,pk=data.account_id,owner=request.auth,
                                    platform=data.platform,connection_status='connected')
    try:
        ZoneInfo(data.timezone)
    except Exception:
        raise HttpError(422,'Invalid timezone.')
    if data.scheduled_at and (timezone.is_naive(data.scheduled_at) or data.scheduled_at <= timezone.now()+timedelta(minutes=10)):
        raise HttpError(422,'Scheduled time must include an offset and be at least ten minutes in the future.')
    post = ScheduledPost.objects.create(owner=request.auth,generated_short=short,social_account=account,
        platform=data.platform,title=data.title,description=data.description,hashtags=data.hashtags,
        timezone=data.timezone,scheduled_at=data.scheduled_at)
    return post_data(post)


def enqueue_social_post(post_id):
    try:
        publish_youtube_post.delay(str(post_id))
    except Exception:
        log.exception('Could not enqueue social post %s',post_id)
        ScheduledPost.objects.filter(pk=post_id,status='queued').update(status='failed',last_error='Social worker is unavailable. Start Redis and the Celery worker, then retry.')


@api.post('/v1/jobs/{job_id}/schedule-batch', response={202: dict})
def schedule_job_batch(request, job_id: uuid.UUID):
    """One explicit user action queues the generated Shorts at 0, 5 and 10 hours."""
    with transaction.atomic():
        job = get_object_or_404(GenerationJob.objects.select_for_update().select_related('source'),
                                pk=job_id, owner=request.auth)
        if job.status not in {'needs_review', 'completed'}:
            raise HttpError(409, 'Wait until generation finishes before scheduling.')
        if not job.source.rights_declaration:
            raise HttpError(409, 'The source has no rights declaration. Use an authorized source.')
        shorts = list(job.shorts.all().order_by('short_number'))
        if not shorts or len(shorts) > 3:
            raise HttpError(409, 'No finished Shorts are available to schedule.')
        if any(not s.video_file or not (settings.MEDIA_ROOT / s.video_file).is_file() or
               s.quality_report.get('transcript_edit_status') == 'rendering' for s in shorts):
            raise HttpError(409, 'A video is missing or still rendering. Try again when it is ready.')
        accounts = list(ConnectedSocialAccount.objects.filter(owner=request.auth, platform='youtube',
                                                               connection_status='connected'))
        if len(accounts) != 1:
            raise HttpError(409, 'Connect exactly one YouTube channel in Schedule before using one-click scheduling.')
        account = accounts[0]
        existing = list(ScheduledPost.objects.select_for_update().filter(
            generated_short__in=shorts, platform='youtube'))
        active = [p for p in existing if p.status in {'queued','uploading','scheduled','published','uploaded_private'}]
        if any(p.social_account_id != account.id for p in active):
            raise HttpError(409, 'A Short is already queued or uploaded to another YouTube channel.')
        if len(active) == len(shorts):
            return 202, {'posts': [post_data(next(p for p in active if p.generated_short_id == s.id)) for s in shorts],
                         'already_scheduled': True, 'already_uploaded': len([p for p in active if p.external_post_id])}
        from .services.groq import enabled as groq_enabled, generate_metadata
        if groq_enabled():
            for short in shorts:
                if short.id not in {post.generated_short_id for post in active} and short.transcript and short.quality_report.get('metadata_source') != 'groq':
                    generated = generate_metadata(short.transcript, job.source.title,
                                                   job.source.source_metadata.get('description') or '')
                    if generated:
                        short.title = generated['title']
                        short.description = generated['description'] + '\n\nSource: ' + (job.source.source_url or job.source.title)
                        short.hashtags = generated['hashtags']
                        short.quality_report = dict(short.quality_report, metadata_source='groq')
                        short.save(update_fields=['title','description','hashtags','quality_report'])
        now = timezone.now()
        posts_to_enqueue = []
        posts = []
        for index, short in enumerate(shorts):
            current = next((p for p in active if p.generated_short_id == short.id), None)
            if current:
                posts.append(current)
                continue
            prior = next((p for p in existing if p.generated_short_id == short.id and
                          (p.social_account_id in {None, account.id})), None)
            if prior is None and any(p.generated_short_id == short.id for p in existing):
                raise HttpError(409, 'A Short already has a draft for another YouTube channel.')
            if prior and prior.external_post_id:
                raise HttpError(409, 'A Short was already uploaded. Open Schedule to check it.')
            # Existing drafts keep their record, but use the current generated metadata and slots.
            post = prior or ScheduledPost(owner=request.auth, generated_short=short, platform='youtube')
            post.social_account = account
            post.title = short.title[:100]
            post.description = short.description
            post.hashtags = short.hashtags[:5]
            post.scheduled_at = now + timedelta(hours=5 * index) if index else None
            post.timezone = 'Asia/Kolkata'
            post.status = 'queued'
            post.approved_at = now
            post.approval_basis = 'source_declaration'
            post.last_error = ''
            post.save()
            posts.append(post)
            posts_to_enqueue.append(post.id)
        transaction.on_commit(lambda: [enqueue_social_post(post_id) for post_id in posts_to_enqueue])
    return 202, {'posts': [post_data(post) for post in posts], 'already_scheduled': False,
                 'already_uploaded': len([post for post in active if post.external_post_id])}


@api.post('/v1/social/posts/{post_id}/approve', response={202: dict})
def approve_post(request, post_id: uuid.UUID):
    post = get_object_or_404(ScheduledPost.objects.select_related('generated_short'),pk=post_id,owner=request.auth)
    if post.status not in ['draft','failed']:
        raise HttpError(409,'Only draft or failed posts can be approved.')
    if not post.generated_short or not (
        post.generated_short.rights_review_status == 'rights_documented' or
        post.approval_basis == 'source_declaration' and post.generated_short.job.source.rights_declaration
    ):
        raise HttpError(409,'Complete and document the final rights review before approving publication.')
    if post.scheduled_at and post.scheduled_at <= timezone.now()+timedelta(minutes=10) and post.status != 'failed':
        raise HttpError(422,'Scheduled time is too close or has passed. Edit the draft before approving.')
    if not post.social_account or post.social_account.connection_status != 'connected':
        raise HttpError(409,'Connect an eligible destination account first.')
    with transaction.atomic():
        if post.status == 'failed' and post.scheduled_at and post.scheduled_at <= timezone.now()+timedelta(minutes=10):
            post.scheduled_at = None
            post.save(update_fields=['scheduled_at'])
        if not ScheduledPost.objects.filter(pk=post.pk,status__in=['draft','failed']).update(status='queued',approved_at=timezone.now(),last_error=''):
            raise HttpError(409,'Post was already approved.')
    enqueue_social_post(post.id)
    post.refresh_from_db()
    return 202, post_data(post)


@api.post('/v1/social/posts/{post_id}/cancel')
def cancel_post(request, post_id: uuid.UUID):
    post = get_object_or_404(ScheduledPost,pk=post_id,owner=request.auth)
    if post.status == 'uploading' or post.external_post_id:
        raise HttpError(409,'Upload has started; manage the video directly in YouTube Studio.')
    ScheduledPost.objects.filter(pk=post.pk,status__in=['draft','queued','failed']).update(status='cancelled')
    post.refresh_from_db()
    return post_data(post)


@api.post('/v1/shorts/{short_id}/rights-review')
def approve_rights(request, short_id: uuid.UUID, permission_verified: bool = False, music_verified: bool = False):
    short = owned_short(request,short_id)
    if not permission_verified or not music_verified:
        raise HttpError(422,'Confirm source permission and music rights separately after reviewing the final video.')
    short.rights_review_status = 'rights_documented'
    short.quality_report = dict(short.quality_report,rights_review=dict(short.quality_report.get('rights_review',{}),
        manual_source_permission_review=True,manual_music_review=True,reviewed_at=timezone.now().isoformat(),
        copyright_permission='User-documented permission; not independently verified'))
    short.save(update_fields=['rights_review_status','quality_report'])
    return short_data(short)


class PostingPreferencesInput(Schema):
    timezone: str = 'Asia/Kolkata'
    slots: list[str] = Field(min_length=1,max_length=8)


@api.get('/v1/social/preferences')
def get_posting_preferences(request):
    pref,_ = PostingPreference.objects.get_or_create(owner=request.auth)
    return {'timezone':pref.timezone,'slots':pref.slots}


@api.put('/v1/social/preferences')
def set_posting_preferences(request,data: PostingPreferencesInput):
    import re
    try:
        ZoneInfo(data.timezone)
    except Exception:
        raise HttpError(422,'Invalid timezone.')
    if any(not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',slot) for slot in data.slots) or len(set(data.slots)) != len(data.slots):
        raise HttpError(422,'Slots must be distinct 24-hour HH:MM times.')
    pref,_ = PostingPreference.objects.update_or_create(owner=request.auth,defaults={'timezone':data.timezone,'slots':sorted(data.slots)})
    return {'timezone':pref.timezone,'slots':pref.slots}


@api.get('/v1/social/suggestions')
def suggest_slots(request, count: int = 3):
    from datetime import datetime as native_datetime
    if count < 1 or count > 10:
        raise HttpError(422,'Count must be between one and ten.')
    pref,_ = PostingPreference.objects.get_or_create(owner=request.auth)
    zone = ZoneInfo(pref.timezone)
    now = timezone.now().astimezone(zone)
    occupied = set(ScheduledPost.objects.filter(owner=request.auth,status__in=['queued','scheduled']).exclude(scheduled_at=None).values_list('scheduled_at',flat=True))
    result = []
    for day in range(30):
        date = now.date()+timedelta(days=day)
        for slot in pref.slots:
            hour, minute = map(int,slot.split(':'))
            candidate = native_datetime.combine(date,native_datetime.min.time().replace(hour=hour,minute=minute),tzinfo=zone)
            utc = candidate.astimezone(ZoneInfo('UTC'))
            if candidate > now+timedelta(minutes=10) and utc not in occupied:
                result.append({'local':candidate.isoformat(),'utc':utc.isoformat()})
                if len(result) == count:
                    return result
    return result


class PostEdit(Schema):
    title: str = Field(min_length=1,max_length=500)
    description: str = Field(max_length=5000)
    hashtags: list[str] = Field(default_factory=list,max_length=5)
    account_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None
    timezone: str = 'Asia/Kolkata'


@api.put('/v1/social/posts/{post_id}')
def update_post(request, post_id: uuid.UUID, data: PostEdit):
    post = get_object_or_404(ScheduledPost,pk=post_id,owner=request.auth)
    if post.status != 'draft':
        raise HttpError(409,'Only drafts can be edited.')
    try:
        ZoneInfo(data.timezone)
    except Exception:
        raise HttpError(422,'Invalid timezone.')
    if data.scheduled_at and (timezone.is_naive(data.scheduled_at) or data.scheduled_at <= timezone.now()+timedelta(minutes=10)):
        raise HttpError(422,'Scheduled time must include an offset and be at least ten minutes in the future.')
    account = None
    if data.account_id:
        account = get_object_or_404(ConnectedSocialAccount,pk=data.account_id,owner=request.auth,
                                    platform=post.platform,connection_status='connected')
        if ScheduledPost.objects.filter(generated_short=post.generated_short,social_account=account,platform=post.platform).exclude(pk=post.pk).exists():
            raise HttpError(409,'This Short already has a post for that account.')
    for field in ['title','description','hashtags','scheduled_at','timezone']:
        setattr(post,field,getattr(data,field))
    post.social_account = account
    post.save()
    return post_data(post)


class BatchApproval(Schema):
    post_ids: list[uuid.UUID] = Field(min_length=1,max_length=10)


@api.post('/v1/social/approve-batch', response={202: list[dict]})
def approve_batch(request, data: BatchApproval):
    ids = data.post_ids
    if len(set(ids)) != len(ids):
        raise HttpError(422,'Each draft may appear only once.')
    with transaction.atomic():
        posts = list(ScheduledPost.objects.select_for_update().select_related('generated_short','social_account').filter(owner=request.auth,pk__in=ids))
        if len(posts) != len(ids):
            raise HttpError(404,'A selected draft was not found.')
        for post in posts:
            if post.status not in ['draft','failed'] or not post.generated_short or post.generated_short.rights_review_status != 'rights_documented':
                raise HttpError(409,'All selected posts need a final documented rights review and draft or failed status.')
            if not post.social_account or post.social_account.connection_status != 'connected':
                raise HttpError(409,'Connect an eligible account for every selected post.')
            if post.scheduled_at and post.scheduled_at <= timezone.now()+timedelta(minutes=10):
                raise HttpError(422,'A scheduled time has passed or is too close. Edit that draft first.')
        ScheduledPost.objects.filter(pk__in=ids).update(status='queued',approved_at=timezone.now(),last_error='')
        transaction.on_commit(lambda: [enqueue_social_post(post.pk) for post in posts])
    for post in posts:
        post.refresh_from_db()
    return 202, [post_data(post) for post in posts]
