"""Official Instagram API with Instagram Login: OAuth, token refresh and Reels publishing.

Verified against Meta's docs (see the WP13 report): Reels need a publicly reachable `video_url`; resumable upload via
rupload.facebook.com is documented only for Facebook Login for Business, so this module uses the video_url path and
requires PUBLIC_MEDIA_BASE_URL. There is no native scheduling parameter; scheduling is done by our beat task.
"""
import logging
import os
import secrets
import shutil
import time
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
import httpx
from django.conf import settings
from django.utils import timezone
from ..services.media import StudioError
from ..services.social import cipher, seal, unseal

log = logging.getLogger(__name__)
AUTHORIZE_URL = 'https://www.instagram.com/oauth/authorize'
SHORT_TOKEN_URL = 'https://api.instagram.com/oauth/access_token'
SCOPES = 'instagram_business_basic,instagram_business_content_publish'
# Graph error codes for throttling / app-level rate limits.
RATE_LIMIT_CODES = {4, 9, 17, 32, 613}


def graph_base():
    return 'https://graph.instagram.com/' + os.getenv('INSTAGRAM_GRAPH_VERSION', 'v27.0')


def env_config():
    return tuple(os.getenv(name, '') for name in
                 ('INSTAGRAM_APP_ID', 'INSTAGRAM_APP_SECRET', 'INSTAGRAM_REDIRECT_URI'))


def configuration_reason():
    """None when configured, else a human-readable reason."""
    missing = [name for name, value in zip(('INSTAGRAM_APP_ID', 'INSTAGRAM_APP_SECRET', 'INSTAGRAM_REDIRECT_URI'), env_config()) if not value]
    if not os.getenv('SOCIAL_ENCRYPTION_KEY', ''):
        missing.append('SOCIAL_ENCRYPTION_KEY')
    return ('Set ' + ', '.join(missing) + ' to enable Instagram.') if missing else None


def is_configured():
    return configuration_reason() is None


def public_base_url():
    value = os.getenv('PUBLIC_MEDIA_BASE_URL', '').strip().rstrip('/')
    if value and urlsplit(value).scheme == 'https' and urlsplit(value).hostname:
        return value
    return ''


def config():
    reason = configuration_reason()
    if reason:
        raise StudioError('instagram_unconfigured', reason)
    cipher()
    app_id, secret, redirect = env_config()
    if urlsplit(redirect).scheme not in {'http', 'https'}:
        raise StudioError('instagram_unconfigured', 'INSTAGRAM_REDIRECT_URI must be an http(s) URL.')
    return app_id, secret, redirect


def authorization_url(state):
    app_id, _, redirect = config()
    return AUTHORIZE_URL + '?' + urlencode({'client_id': app_id, 'redirect_uri': redirect, 'response_type': 'code',
                                            'scope': SCOPES, 'state': state})


# ---- HTTP ------------------------------------------------------------------------------------------------------

def _call(method, url, *, token=None, **kwargs):
    """One Graph call. Transient failures (429/5xx/transport) raise httpx errors so the task can retry;
    permanent rejections become StudioError carrying Meta's own message."""
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    response = httpx.request(method, url, headers=headers, timeout=kwargs.pop('timeout', 30), **kwargs)
    if response.status_code >= 400:
        if response.status_code == 429 or response.status_code >= 500:
            response.raise_for_status()
        try:
            error = response.json().get('error', {})
        except ValueError:
            error = {}
        message = error.get('error_user_msg') or error.get('message') or f'HTTP {response.status_code}'
        if error.get('code') in RATE_LIMIT_CODES:
            raise StudioError('instagram_rate_limited', 'Instagram rate limit reached: ' + message)
        raise StudioError('instagram_api_error', 'Instagram rejected the request: ' + message)
    return response.json()


# ---- OAuth -----------------------------------------------------------------------------------------------------

def exchange_code(code):
    """Authorization code -> short-lived token -> 60-day long-lived token."""
    app_id, secret, redirect = config()
    short = _call('POST', SHORT_TOKEN_URL, data={'client_id': app_id, 'client_secret': secret,
        'grant_type': 'authorization_code', 'redirect_uri': redirect, 'code': code})
    entry = (short.get('data') or [short])[0]
    if not entry.get('access_token'):
        raise StudioError('oauth_failed', 'Instagram did not return an access token.')
    long = _call('GET', 'https://graph.instagram.com/access_token', params={'grant_type': 'ig_exchange_token',
        'client_secret': secret, 'access_token': entry['access_token']})
    if not long.get('access_token'):
        raise StudioError('oauth_failed', 'Instagram did not return a long-lived token.')
    return {'access_token': long['access_token'], 'expires_in': long.get('expires_in', 5183944),
            'user_id': str(entry.get('user_id', ''))}


def identity(token):
    data = _call('GET', graph_base() + '/me', token=token, params={'fields': 'user_id,username'})
    ig_id = str(data.get('user_id') or data.get('id') or '')
    if not ig_id:
        raise StudioError('instagram_account_missing', 'No Instagram professional account is available for this login.')
    return ig_id, data.get('username') or ig_id


def current_token(account):
    """Return a valid token, refreshing when within 7 days of expiry (needs the token >=24h old, per Meta)."""
    data = unseal(account.encrypted_credentials)
    now = timezone.now()
    if account.token_expiry and account.token_expiry > now + timedelta(days=7):
        return data['access_token']
    try:
        refreshed = _call('GET', 'https://graph.instagram.com/refresh_access_token',
                          params={'grant_type': 'ig_refresh_token', 'access_token': data['access_token']})
        data['access_token'] = refreshed['access_token']
        account.encrypted_credentials = seal(data)
        account.token_expiry = now + timedelta(seconds=refreshed.get('expires_in', 5183944))
        account.save(update_fields=['encrypted_credentials', 'token_expiry'])
    except (StudioError, httpx.HTTPError, KeyError):
        if account.token_expiry and account.token_expiry > now:
            return data['access_token']
        raise StudioError('instagram_token_expired', 'The Instagram login expired. Reconnect Instagram.')
    return data['access_token']


def refresh_expiring_tokens():
    from ..models import ConnectedSocialAccount
    for account in ConnectedSocialAccount.objects.filter(platform='instagram', connection_status='connected',
            token_expiry__lte=timezone.now() + timedelta(days=7)):
        try:
            current_token(account)
        except Exception:
            log.warning('Could not refresh Instagram token for account %s', account.pk)


# ---- publishing --------------------------------------------------------------------------------------------------

def publishing_quota(token, ig_id):
    """(used, total) from GET /{ig-id}/content_publishing_limit; (0, None) when unreadable."""
    data = _call('GET', f'{graph_base()}/{ig_id}/content_publishing_limit', token=token,
                 params={'fields': 'quota_usage,config'})
    row = (data.get('data') or [{}])[0]
    return row.get('quota_usage', 0), (row.get('config') or {}).get('quota_total')


def stage_public_copy(video_path):
    """Copy the video under an unguessable name into MEDIA_ROOT/public_reels, the only folder that must be web-exposed."""
    base = public_base_url()
    if not base:
        raise StudioError('public_media_unconfigured',
            'Instagram needs a public https URL for the video. Set PUBLIC_MEDIA_BASE_URL to a server that exposes MEDIA_ROOT/public_reels.')
    folder = settings.MEDIA_ROOT / 'public_reels'
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_urlsafe(24) + '.mp4'
    shutil.copyfile(video_path, folder / name)
    return f'{base}/{name}', folder / name


def create_container(token, ig_id, video_url, caption, share_to_feed):
    data = _call('POST', f'{graph_base()}/{ig_id}/media', token=token, data={'media_type': 'REELS',
        'video_url': video_url, 'caption': caption, 'share_to_feed': 'true' if share_to_feed else 'false'})
    if not data.get('id'):
        raise StudioError('instagram_api_error', 'Instagram did not return a media container id.')
    return data['id']


def wait_for_container(token, container_id):
    """Bounded polling: INSTAGRAM_POLL_TIMEOUT (default 300s, Meta documents up to 5 minutes)."""
    timeout = float(os.getenv('INSTAGRAM_POLL_TIMEOUT', 300))
    interval = float(os.getenv('INSTAGRAM_POLL_INTERVAL', 15))
    deadline = time.monotonic() + timeout
    while True:
        data = _call('GET', f'{graph_base()}/{container_id}', token=token, params={'fields': 'status_code,status'})
        code = data.get('status_code')
        if code in {'FINISHED', 'PUBLISHED'}:
            return
        if code in {'ERROR', 'EXPIRED'}:
            raise StudioError('instagram_container_failed',
                f'Instagram could not process the video ({code}): {data.get("status") or "no detail provided"}.')
        if time.monotonic() >= deadline:
            raise StudioError('instagram_processing_timeout',
                f'Instagram was still processing the video after {int(timeout)} seconds. Retry later.')
        time.sleep(interval)


def publish_container(token, ig_id, container_id):
    data = _call('POST', f'{graph_base()}/{ig_id}/media_publish', token=token, data={'creation_id': container_id})
    if not data.get('id'):
        raise StudioError('instagram_api_error', 'Instagram did not confirm a published media id.')
    return data['id']


def media_permalink(token, media_id):
    data = _call('GET', f'{graph_base()}/{media_id}', token=token, params={'fields': 'permalink'})
    return data.get('permalink', '')


def public_media_ready():
    return bool(public_base_url())


def publishing_available():
    """Studio-level readiness (no account check): app configured and a public media URL set.
    GET /v1/social/platforms adds the per-user 'connected' condition for can_publish."""
    return is_configured() and public_media_ready()
