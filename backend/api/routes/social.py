import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo
from django.conf import settings
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field
from ..models import GenerationJob, ConnectedSocialAccount, ScheduledPost, PostingPreference
from ..services.media import StudioError
from ..services.social import authorization_url, exchange_code, youtube_identity, seal
from ..tasks import publish_youtube_post
from ..publishing import instagram
from ..publishing import metadata as pubmeta
from ..publishing.tasks import publish_instagram_post
from .base import owned_short

log = logging.getLogger(__name__)
router = Router()


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
            'approval_basis':post.approval_basis,'platform_metadata':post.platform_metadata}


@router.get('/v1/social/accounts')
def social_accounts(request):
    return [account_data(a) for a in ConnectedSocialAccount.objects.filter(owner=request.auth,connection_status='connected')]


@router.get('/v1/social/youtube/connect')
def youtube_connect(request):
    state = secrets.token_urlsafe(32)
    request.session['youtube_oauth_state'] = state
    from urllib.parse import urlsplit
    referer = request.headers.get('Referer','')
    parsed = urlsplit(referer)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    request.session['social_return_origin'] = origin if origin in settings.CORS_ALLOWED_ORIGINS else os.getenv('SOCIAL_FRONTEND_URL','http://localhost:5173')
    return {'authorization_url':authorization_url(state)}


@router.get('/v1/social/youtube/callback')
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


@router.get('/v1/social/platforms')
def social_platforms(request):
    connected = set(ConnectedSocialAccount.objects.filter(owner=request.auth,connection_status='connected').values_list('platform',flat=True))
    yt_missing = [n for n in ('YOUTUBE_CLIENT_ID','YOUTUBE_CLIENT_SECRET','YOUTUBE_REDIRECT_URI','SOCIAL_ENCRYPTION_KEY') if not os.getenv(n,'')]
    yt_configured = not yt_missing
    ig_reason = instagram.configuration_reason()
    ig_configured = ig_reason is None
    if ig_configured and not instagram.public_media_ready():
        ig_publish_reason = 'Set PUBLIC_MEDIA_BASE_URL to a public https address that serves MEDIA_ROOT/public_reels; Instagram must fetch the video from a public URL.'
    elif ig_configured and 'instagram' not in connected:
        ig_publish_reason = 'Connect an Instagram professional account.'
    else:
        ig_publish_reason = ig_reason
    return {
        'youtube':{'configured':yt_configured,'connected':'youtube' in connected,
                   'can_publish':yt_configured and 'youtube' in connected,
                   'reason':('Set '+', '.join(yt_missing)+'.') if yt_missing else None if 'youtube' in connected else 'Connect a YouTube channel.'},
        'instagram':{'configured':ig_configured,'connected':'instagram' in connected,
                     'can_publish':instagram.publishing_available() and 'instagram' in connected,
                     'reason':ig_publish_reason}}


@router.get('/v1/social/instagram/connect')
def instagram_connect(request):
    state = secrets.token_urlsafe(32)
    url = instagram.authorization_url(state)
    request.session['instagram_oauth_state'] = state
    from urllib.parse import urlsplit
    parsed = urlsplit(request.headers.get('Referer',''))
    origin = f'{parsed.scheme}://{parsed.netloc}'
    request.session['social_return_origin'] = origin if origin in settings.CORS_ALLOWED_ORIGINS else os.getenv('SOCIAL_FRONTEND_URL','http://localhost:5173')
    return {'authorization_url':url}


@router.get('/v1/social/instagram/callback')
def instagram_callback(request, code: str = '', state: str = '', error: str = ''):
    saved = request.session.pop('instagram_oauth_state',None)
    if error or not code or not saved or not secrets.compare_digest(saved,state):
        raise StudioError('oauth_rejected','Instagram authorization was rejected or expired. Try connecting again.')
    try:
        token = instagram.exchange_code(code.split('#')[0])
        ig_id,name = instagram.identity(token['access_token'])
    except StudioError:
        raise
    except Exception:
        raise StudioError('oauth_failed','Could not complete Instagram authorization. Verify app credentials and redirect URI.')
    ConnectedSocialAccount.objects.update_or_create(owner=request.auth,platform='instagram',
        platform_account_id=ig_id,defaults={'account_name':name,'encrypted_credentials':seal({'access_token':token['access_token']}),
        'token_expiry':timezone.now()+timedelta(seconds=token.get('expires_in',5183944)),'connection_status':'connected'})
    return HttpResponseRedirect(request.session.pop('social_return_origin',os.getenv('SOCIAL_FRONTEND_URL','http://localhost:5173'))+'/?view=schedule&connected=instagram')


@router.post('/v1/social/accounts/{account_id}/disconnect')
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
    hashtags: list[str] = Field(default_factory=list,max_length=30)
    timezone: str = 'Asia/Kolkata'
    scheduled_at: datetime | None = None


@router.get('/v1/social/posts')
def posts(request):
    return [post_data(p) for p in ScheduledPost.objects.filter(owner=request.auth).select_related('generated_short')[:100]]


@router.post('/v1/social/posts')
def save_draft(request, data: DraftInput):
    short = owned_short(request,data.short_id)
    if data.platform == 'instagram' and not instagram.is_configured():
        raise StudioError('instagram_unavailable','Instagram connection and publishing require an eligible professional account and public media hosting; this studio has neither configured.')
    if data.platform == 'youtube' and len(data.hashtags) > pubmeta.YOUTUBE_MAX_HASHTAGS:
        raise HttpError(422,'YouTube posts allow at most five hashtags.')
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
    try:
        meta,_ = pubmeta.build_metadata(short,data.platform,title=data.title,description=data.description,
                                        hashtags=data.hashtags,scheduled=bool(data.scheduled_at))
    except pubmeta.MetadataError as exc:
        raise HttpError(422,str(exc))
    post = ScheduledPost.objects.create(owner=request.auth,generated_short=short,social_account=account,
        platform=data.platform,title=data.title,description=data.description,hashtags=data.hashtags,
        timezone=data.timezone,scheduled_at=data.scheduled_at,platform_metadata={data.platform:meta})
    return post_data(post)


def enqueue_social_post(post_id):
    platform = ScheduledPost.objects.filter(pk=post_id).values_list('platform',flat=True).first()
    if platform == 'instagram':
        return enqueue_instagram_post(post_id)
    try:
        publish_youtube_post.delay(str(post_id))
    except Exception:
        log.exception('Could not enqueue social post %s',post_id)
        ScheduledPost.objects.filter(pk=post_id,status='queued').update(status='failed',last_error='Social worker is unavailable. Start Redis and the Celery worker, then retry.')


def enqueue_instagram_post(post_id):
    """Instagram has no native scheduling: future posts wait as 'scheduled' for the beat task; others publish now."""
    post = ScheduledPost.objects.filter(pk=post_id,status='queued').first()
    if not post:
        return
    if post.scheduled_at and post.scheduled_at > timezone.now():
        ScheduledPost.objects.filter(pk=post_id,status='queued').update(status='scheduled')
        return
    try:
        publish_instagram_post.delay(str(post_id))
    except Exception:
        log.exception('Could not enqueue Instagram post %s',post_id)
        ScheduledPost.objects.filter(pk=post_id,status='queued').update(status='failed',last_error='Social worker is unavailable. Start Redis and the Celery worker, then retry.')


def prefetch_batch_metadata(owner, job_id):
    """Best-effort Groq metadata for Shorts that will need it. Runs without any transaction or row lock."""
    from ..services.groq import enabled as groq_enabled, generate_metadata
    if not groq_enabled():
        return {}
    job = GenerationJob.objects.filter(pk=job_id,owner=owner).first()
    if not job or job.status not in {'needs_review','completed'}:
        return {}
    shorts = list(job.shorts.all().order_by('short_number'))
    if not shorts or len(shorts) > 3:
        return {}
    active = set(ScheduledPost.objects.filter(generated_short__in=shorts,platform='youtube',
        status__in=['queued','uploading','scheduled','published','uploaded_private']).values_list('generated_short_id',flat=True))
    results = {}
    for short in shorts:
        if short.id not in active and short.transcript and short.quality_report.get('metadata_source') != 'groq':
            generated = generate_metadata(short.transcript)
            if generated:
                results[short.id] = generated
    return results


@router.post('/v1/jobs/{job_id}/schedule-batch', response={202: dict})
def schedule_job_batch(request, job_id: uuid.UUID):
    """One explicit user action queues the generated Shorts at 0, 5 and 10 hours."""
    generated_metadata = prefetch_batch_metadata(request.auth, job_id)  # network calls stay outside the write lock
    with transaction.atomic():
        job = get_object_or_404(GenerationJob.objects.select_for_update().select_related('source'),
                                pk=job_id, owner=request.auth)
        if job.status not in {'needs_review', 'completed'}:
            raise HttpError(409, 'Wait until generation finishes before scheduling.')
        if not job.source or not job.source.rights_declaration:
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
        from ..services.groq import enabled as groq_enabled, finalize_description
        if groq_enabled():
            for short in shorts:
                if short.id not in {post.generated_short_id for post in active} and short.transcript and short.quality_report.get('metadata_source') != 'groq':
                    generated = generated_metadata.get(short.id)
                    if generated:
                        short.title = generated['title']
                        short.description = finalize_description(generated['description'], generated['hashtags'])
                        short.hashtags = generated['hashtags']
                        short.quality_report = dict(short.quality_report, metadata_source='groq')
                        short.save(update_fields=['title','description','hashtags','quality_report'])
        for short in shorts:
            if short.id in {post.generated_short_id for post in active}:
                continue
            cleaned = finalize_description(short.description, short.hashtags)
            if cleaned != short.description:
                short.description = cleaned
                short.save(update_fields=['description'])
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
            post.description = finalize_description(short.description, short.hashtags)
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


def prepare_for_approval(post):
    """Platform checks before queueing: Instagram needs public media hosting and valid caption metadata;
    YouTube descriptions gain any required asset attribution."""
    short = post.generated_short
    if post.platform == 'instagram':
        if not instagram.is_configured():
            raise HttpError(409,'Instagram is not configured on this studio.')
        if not instagram.public_media_ready():
            raise HttpError(409,'Instagram needs PUBLIC_MEDIA_BASE_URL: a public https address serving MEDIA_ROOT/public_reels.')
        try:
            meta = pubmeta.validate_instagram(post.platform_metadata.get('instagram') or
                                              pubmeta.build_metadata(short,'instagram',title=post.title,description=post.description,hashtags=post.hashtags)[0])
            meta['caption'] = pubmeta.with_attribution(meta['caption'],pubmeta.attributions_for(short))
        except pubmeta.MetadataError as exc:
            raise HttpError(422,str(exc))
        post.platform_metadata = dict(post.platform_metadata,instagram=meta)
        post.save(update_fields=['platform_metadata'])
    elif post.platform == 'youtube':
        missing = [a for a in pubmeta.attributions_for(short) if a not in post.description]
        if missing:
            post.description = (post.description.rstrip()+'\n\n'+pubmeta._attribution_line(missing))[:5000]
            post.save(update_fields=['description'])


@router.post('/v1/social/posts/{post_id}/approve', response={202: dict})
def approve_post(request, post_id: uuid.UUID):
    post = get_object_or_404(ScheduledPost.objects.select_related('generated_short'),pk=post_id,owner=request.auth)
    if post.status not in ['draft','failed']:
        raise HttpError(409,'Only draft or failed posts can be approved.')
    blocker = pubmeta.rights_blocker(post.generated_short,post.approval_basis)
    if blocker:
        raise HttpError(409,blocker[1])
    if post.scheduled_at and post.scheduled_at <= timezone.now()+timedelta(minutes=10) and post.status != 'failed':
        raise HttpError(422,'Scheduled time is too close or has passed. Edit the draft before approving.')
    if not post.social_account or post.social_account.connection_status != 'connected':
        raise HttpError(409,'Connect an eligible destination account first.')
    prepare_for_approval(post)
    with transaction.atomic():
        if post.status == 'failed' and post.scheduled_at and post.scheduled_at <= timezone.now()+timedelta(minutes=10):
            post.scheduled_at = None
            post.save(update_fields=['scheduled_at'])
        if not ScheduledPost.objects.filter(pk=post.pk,status__in=['draft','failed']).update(status='queued',approved_at=timezone.now(),last_error=''):
            raise HttpError(409,'Post was already approved.')
    enqueue_social_post(post.id)
    post.refresh_from_db()
    return 202, post_data(post)


@router.post('/v1/social/posts/{post_id}/cancel')
def cancel_post(request, post_id: uuid.UUID):
    post = get_object_or_404(ScheduledPost,pk=post_id,owner=request.auth)
    if post.status == 'uploading' or post.external_post_id:
        raise HttpError(409,'Upload has started; manage the video directly in YouTube Studio.')
    ScheduledPost.objects.filter(pk=post.pk,status__in=['draft','queued','failed','scheduled']).update(status='cancelled')
    post.refresh_from_db()
    return post_data(post)




class PostingPreferencesInput(Schema):
    timezone: str = 'Asia/Kolkata'
    slots: list[str] = Field(min_length=1,max_length=8)


@router.get('/v1/social/preferences')
def get_posting_preferences(request):
    pref,_ = PostingPreference.objects.get_or_create(owner=request.auth)
    return {'timezone':pref.timezone,'slots':pref.slots}


@router.put('/v1/social/preferences')
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


@router.get('/v1/social/suggestions')
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
    hashtags: list[str] = Field(default_factory=list,max_length=30)
    account_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None
    timezone: str = 'Asia/Kolkata'


@router.put('/v1/social/posts/{post_id}')
def update_post(request, post_id: uuid.UUID, data: PostEdit):
    post = get_object_or_404(ScheduledPost,pk=post_id,owner=request.auth)
    if post.status != 'draft':
        raise HttpError(409,'Only drafts can be edited.')
    if len(data.hashtags) > (30 if post.platform == 'instagram' else pubmeta.YOUTUBE_MAX_HASHTAGS):
        raise HttpError(422,'Too many hashtags for this platform.')
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


@router.post('/v1/social/approve-batch', response={202: list[dict]})
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
            if pubmeta.rights_blocker(post.generated_short,post.approval_basis):
                raise HttpError(409,'A selected post has an unresolved rights or asset licence issue.')
            if not post.social_account or post.social_account.connection_status != 'connected':
                raise HttpError(409,'Connect an eligible account for every selected post.')
            if post.scheduled_at and post.scheduled_at <= timezone.now()+timedelta(minutes=10):
                raise HttpError(422,'A scheduled time has passed or is too close. Edit that draft first.')
        for post in posts:
            prepare_for_approval(post)
        ScheduledPost.objects.filter(pk__in=ids).update(status='queued',approved_at=timezone.now(),last_error='')
        transaction.on_commit(lambda: [enqueue_social_post(post.pk) for post in posts])
    for post in posts:
        post.refresh_from_db()
    return 202, [post_data(post) for post in posts]


class MetadataEdit(Schema):
    caption: str | None = Field(default=None,max_length=5000)
    hashtags: list[str] | None = Field(default=None,max_length=40)
    question: str | None = Field(default=None,max_length=1000)
    share_to_feed: bool | None = None
    title: str | None = Field(default=None,max_length=500)
    description: str | None = Field(default=None,max_length=6000)
    altered_content: bool | None = None


def _limits(platform):
    if platform == 'instagram':
        return {'caption_max':pubmeta.INSTAGRAM_CAPTION_MAX,'hashtags_max':pubmeta.INSTAGRAM_MAX_HASHTAGS}
    return {'title_max':pubmeta.YOUTUBE_TITLE_MAX,'hashtags_max':pubmeta.YOUTUBE_MAX_HASHTAGS}


@router.get('/v1/social/posts/{post_id}/metadata')
def preview_metadata(request, post_id: uuid.UUID, regenerate: bool = False):
    """Preview per-platform metadata. Never persists; uses Groq when enabled, else a deterministic extractive fallback."""
    post = get_object_or_404(ScheduledPost.objects.select_related('generated_short__script'),pk=post_id,owner=request.auth)
    stored = post.platform_metadata.get(post.platform)
    if stored and not regenerate:
        source = 'saved'
        meta = stored
    else:
        try:
            meta,source = pubmeta.build_metadata(post.generated_short,post.platform,title=post.title,description=post.description,
                hashtags=post.hashtags,scheduled=bool(post.scheduled_at),use_groq=True)
        except pubmeta.MetadataError as exc:
            raise HttpError(422,str(exc))
    return {'platform':post.platform,'metadata':meta,'source':source,'limits':_limits(post.platform),
            'required_attribution':pubmeta.attributions_for(post.generated_short)}


@router.put('/v1/social/posts/{post_id}/metadata')
def edit_metadata(request, post_id: uuid.UUID, data: MetadataEdit):
    post = get_object_or_404(ScheduledPost.objects.select_related('generated_short__script'),pk=post_id,owner=request.auth)
    if post.status not in {'draft','failed'} or post.external_post_id:
        raise HttpError(409,'Only drafts or failed posts without an upload can be edited.')
    base = dict(post.platform_metadata.get(post.platform) or
                pubmeta.build_metadata(post.generated_short,post.platform,title=post.title,description=post.description,hashtags=post.hashtags)[0])
    fields = ['caption','hashtags','question','share_to_feed'] if post.platform == 'instagram' else ['title','description','hashtags','altered_content']
    for name in fields:
        value = getattr(data,name)
        if value is not None:
            base[name] = value
    try:
        if post.platform == 'instagram':
            meta = pubmeta.validate_instagram(base)
            meta['caption'] = pubmeta.with_attribution(meta['caption'],pubmeta.attributions_for(post.generated_short))
        else:
            meta = pubmeta.validate_youtube(base)
    except pubmeta.MetadataError as exc:
        raise HttpError(422,str(exc))
    post.platform_metadata = dict(post.platform_metadata,**{post.platform:meta})
    fields_to_save = ['platform_metadata']
    if post.platform == 'youtube':
        post.title,post.description,post.hashtags = meta['title'],meta['description'],meta['hashtags']
        fields_to_save += ['title','description','hashtags']
    post.save(update_fields=fields_to_save)
    return post_data(post)
