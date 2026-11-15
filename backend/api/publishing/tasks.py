import logging
import httpx
from celery import shared_task
from django.conf import settings
from django.utils import timezone
from ..models import ScheduledPost
from ..services.media import StudioError
from ..services.social import seal, unseal
from . import instagram
from .metadata import MetadataError, rights_blocker, attributions_for, validate_instagram, with_attribution

log = logging.getLogger(__name__)


def _fail(post, message, update_session=False):
    post.status = 'failed'
    post.last_error = message
    fields = ['status', 'last_error', 'retry_count']
    if update_session:
        post.encrypted_upload_session = ''
        fields.append('encrypted_upload_session')
    post.save(update_fields=fields)


@shared_task(name='api.publishing.publish_instagram_post')
def publish_instagram_post(post_id):
    # Atomic claim: duplicate deliveries cannot publish the same post twice.
    if not ScheduledPost.objects.filter(pk=post_id, status='queued', approved_at__isnull=False,
                                        platform='instagram').update(status='uploading'):
        return
    post = ScheduledPost.objects.select_related('generated_short__job__source', 'generated_short__script', 'social_account').get(pk=post_id)
    staged = None
    try:
        account = post.social_account
        if not account or account.connection_status != 'connected' or account.platform != 'instagram':
            raise StudioError('account_unavailable', 'A connected Instagram account is required.')
        blocker = rights_blocker(post.generated_short, post.approval_basis)
        if blocker:
            raise StudioError(*blocker)
        token = instagram.current_token(account)
        if post.external_post_id:
            confirm_permalink(post, token)
            return
        meta = validate_instagram(post.platform_metadata.get('instagram') or {})
        caption = with_attribution(meta['caption'], attributions_for(post.generated_short))
        used, total = instagram.publishing_quota(token, account.platform_account_id)
        if total is not None and used >= total:
            raise StudioError('instagram_rate_limited',
                f'Instagram publishing limit reached ({used}/{total} posts in 24 hours). Try again later.')
        container = unseal(post.encrypted_upload_session).get('container_id') if post.encrypted_upload_session else None
        if not container:
            video = settings.MEDIA_ROOT / post.generated_short.video_file
            if not video.is_file():
                raise StudioError('video_expired', 'The approved video expired; render it again before publishing.')
            video_url, staged = instagram.stage_public_copy(video)
            container = instagram.create_container(token, account.platform_account_id, video_url, caption, meta['share_to_feed'])
            post.encrypted_upload_session = seal({'container_id': container})
            post.save(update_fields=['encrypted_upload_session'])
        instagram.wait_for_container(token, container)
        media_id = instagram.publish_container(token, account.platform_account_id, container)
        post.external_post_id = media_id
        post.encrypted_upload_session = ''
        post.save(update_fields=['external_post_id', 'encrypted_upload_session'])
        confirm_permalink(post, token)
    except MetadataError as exc:
        _fail(post, str(exc))
    except Exception as exc:
        log.exception('Instagram publish failed for post %s', post_id)
        post.retry_count += 1
        status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
        transient = isinstance(exc, httpx.TransportError) or status_code in {429, 500, 502, 503, 504}
        if transient and post.retry_count < 3:
            post.status = 'queued'
            post.last_error = 'Temporary Instagram error. Retrying automatically.'
            post.save(update_fields=['retry_count', 'status', 'last_error'])
            try:
                publish_instagram_post.apply_async(args=[post_id], countdown=30 * 2 ** (post.retry_count - 1))
                return
            except Exception:
                log.exception('Could not enqueue Instagram retry for post %s', post_id)
        code = getattr(exc, 'code', '')
        if code == 'instagram_rate_limited' and post.scheduled_at and not post.external_post_id:
            # Stay in the scheduler's queue; the next beat tick retries once the window frees up.
            post.status = 'scheduled'
            post.last_error = exc.message
            post.save(update_fields=['retry_count', 'status', 'last_error'])
            return
        clear_session = code in {'instagram_container_failed', 'instagram_processing_timeout'}
        _fail(post, getattr(exc, 'message', 'Instagram publishing failed. Review the account, rate limit and worker logs.'),
              update_session=clear_session)
    finally:
        if staged:
            staged.unlink(missing_ok=True)


def confirm_permalink(post, token):
    permalink = instagram.media_permalink(token, post.external_post_id)
    if not permalink:
        raise StudioError('instagram_permalink_missing',
            f'Instagram returned media id {post.external_post_id} but no permalink. Approve again to re-check.')
    post.published_url = permalink
    post.status = 'published'
    post.published_at = timezone.now()
    post.last_error = ''
    post.save(update_fields=['published_url', 'status', 'published_at', 'last_error'])


@shared_task(name='api.publishing.publish_due_instagram_posts')
def publish_due_instagram_posts():
    """Instagram has no native scheduling: publish approved posts once scheduled_at has passed."""
    due = list(ScheduledPost.objects.filter(platform='instagram', status='scheduled', approved_at__isnull=False,
                                            scheduled_at__lte=timezone.now()).values_list('pk', flat=True)[:20])
    queued = 0
    for pk in due:
        if ScheduledPost.objects.filter(pk=pk, status='scheduled').update(status='queued'):
            try:
                publish_instagram_post.delay(str(pk))
                queued += 1
            except Exception:
                log.exception('Could not enqueue due Instagram post %s', pk)
                ScheduledPost.objects.filter(pk=pk, status='queued').update(status='scheduled')
    return queued


@shared_task(name='api.publishing.refresh_instagram_tokens')
def refresh_instagram_tokens():
    instagram.refresh_expiring_tokens()
