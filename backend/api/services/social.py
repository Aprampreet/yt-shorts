"""Official Google OAuth and resumable YouTube upload; credentials stay encrypted."""
import json
import os
from datetime import timedelta
from urllib.parse import urlencode, urlsplit
import httpx
from cryptography.fernet import Fernet
from django.utils import timezone
from .media import StudioError

GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'
YOUTUBE_API = 'https://www.googleapis.com/youtube/v3'
UPLOAD_API = 'https://www.googleapis.com/upload/youtube/v3/videos'
SCOPE = 'https://www.googleapis.com/auth/youtube.upload'


def cipher():
    key = os.getenv('SOCIAL_ENCRYPTION_KEY','')
    if not key:
        raise StudioError('social_key_missing','Set SOCIAL_ENCRYPTION_KEY before connecting social accounts.')
    try:
        return Fernet(key.encode())
    except ValueError:
        raise StudioError('social_key_invalid','SOCIAL_ENCRYPTION_KEY must be a valid Fernet key.')


def seal(data):
    return cipher().encrypt(json.dumps(data).encode()).decode()


def unseal(value):
    return json.loads(cipher().decrypt(value.encode()))


def youtube_config():
    cid, secret, redirect = (os.getenv(name,'') for name in ['YOUTUBE_CLIENT_ID','YOUTUBE_CLIENT_SECRET','YOUTUBE_REDIRECT_URI'])
    if not all([cid,secret,redirect]) or urlsplit(redirect).scheme not in {'https','http'}:
        raise StudioError('youtube_unconfigured','Configure YouTube OAuth client ID, secret and exact redirect URI.')
    cipher()
    return cid, secret, redirect


def authorization_url(state):
    cid, _, redirect = youtube_config()
    return GOOGLE_AUTH+'?'+urlencode({'client_id':cid,'redirect_uri':redirect,'response_type':'code',
        'scope':SCOPE+' https://www.googleapis.com/auth/youtube.readonly','access_type':'offline','prompt':'consent','state':state})


def exchange_code(code):
    cid, secret, redirect = youtube_config()
    response = httpx.post(GOOGLE_TOKEN,data={'code':code,'client_id':cid,'client_secret':secret,
        'redirect_uri':redirect,'grant_type':'authorization_code'},timeout=20)
    response.raise_for_status()
    data = response.json()
    if not data.get('refresh_token'):
        raise StudioError('refresh_token_missing','Google did not return offline access. Reconnect and grant consent.')
    return data


def youtube_identity(access_token):
    response = httpx.get(YOUTUBE_API+'/channels',params={'part':'snippet','mine':'true'},
        headers={'Authorization':'Bearer '+access_token},timeout=20)
    response.raise_for_status()
    items = response.json().get('items',[])
    if not items:
        raise StudioError('youtube_channel_missing','No YouTube channel is available for this account.')
    return items[0]['id'], items[0]['snippet']['title']


def current_token(account):
    data = unseal(account.encrypted_credentials)
    if account.token_expiry and account.token_expiry > timezone.now()+timedelta(minutes=2):
        return data['access_token']
    cid, secret, _ = youtube_config()
    response = httpx.post(GOOGLE_TOKEN,data={'client_id':cid,'client_secret':secret,
        'refresh_token':data['refresh_token'],'grant_type':'refresh_token'},timeout=20)
    response.raise_for_status()
    refreshed = response.json()
    data['access_token'] = refreshed['access_token']
    account.encrypted_credentials = seal(data)
    account.token_expiry = timezone.now()+timedelta(seconds=refreshed.get('expires_in',3600))
    account.save(update_fields=['encrypted_credentials','token_expiry'])
    return data['access_token']


def youtube_upload(post, path):
    """Persist the official resumable session before sending bytes. Retry queries it first."""
    token = current_token(post.social_account)
    headers = {'Authorization':'Bearer '+token}
    size = path.stat().st_size
    if post.encrypted_upload_session:
        session = unseal(post.encrypted_upload_session)['uri']
    else:
        status = {'privacyStatus':'private' if post.scheduled_at else 'public'}
        if post.scheduled_at:
            status['publishAt'] = post.scheduled_at.isoformat().replace('+00:00','Z')
        visible_tags = ' '.join(tag for tag in post.hashtags if tag.startswith('#'))
        description = post.description.strip()
        if visible_tags and visible_tags not in description:
            description = (description + '\n\n' + visible_tags)[:5000]
        body = {'snippet':{'title':post.title[:100],'description':description[:5000],
                           'tags':[tag.lstrip('#') for tag in post.hashtags]}, 'status':status}
        # Disclosure of realistic altered/synthetic content: videos resource property status.containsSyntheticMedia
        # (boolean, writable on videos.insert; confirmed in the YouTube Data API reference).
        if (post.platform_metadata or {}).get('youtube', {}).get('altered_content'):
            status['containsSyntheticMedia'] = True
        with httpx.Client(timeout=30) as client:
            response = client.post(UPLOAD_API,params={'uploadType':'resumable','part':'snippet,status'},
                headers={**headers,'X-Upload-Content-Type':'video/mp4','X-Upload-Content-Length':str(size)},json=body)
            response.raise_for_status()
        session = response.headers.get('Location','')
        if not session:
            raise StudioError('upload_session_missing','YouTube did not create an upload session.')
        post.encrypted_upload_session = seal({'uri':session})
        post.save(update_fields=['encrypted_upload_session'])
    parsed = urlsplit(session)
    if parsed.scheme != 'https' or parsed.hostname not in {'www.googleapis.com','youtube.googleapis.com'}:
        raise StudioError('upload_session_invalid','YouTube returned an unexpected upload destination.')
    with httpx.Client(timeout=120) as client:
        status = client.put(session,headers={**headers,'Content-Range':f'bytes */{size}','Content-Length':'0'})
        if status.status_code == 200 or status.status_code == 201:
            return status.json()['id'], status.json().get('status',{}).get('privacyStatus','unknown')
        if status.status_code not in {308,404}:
            status.raise_for_status()
        if status.status_code == 404:
            raise StudioError('upload_session_expired','YouTube upload session expired. Review and start a new draft.')
        last = status.headers.get('Range','')
        offset = int(last.rsplit('-',1)[-1])+1 if last else 0
        with path.open('rb') as media:
            media.seek(offset)
            response = client.put(session,headers={**headers,'Content-Type':'video/mp4',
                'Content-Range':f'bytes {offset}-{size-1}/{size}'},content=media)
        response.raise_for_status()
        if response.status_code == 308:
            raise StudioError('upload_incomplete','YouTube accepted only part of the upload. Retry to resume.')
        return response.json()['id'], response.json().get('status',{}).get('privacyStatus','unknown')
