import os
import shutil
import uuid
from pathlib import Path
from django.conf import settings
from django.middleware.csrf import get_token
from ninja import Router

router = Router()


@router.get('/v1/session', auth=None)
def session(request):
    if 'studio_owner' not in request.session:
        request.session['studio_owner'] = str(uuid.uuid4())
    return {'csrf_token': get_token(request)}


@router.get('/v1/capabilities', auth=None)
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


@router.get('/v1/health', auth=None)
def health(request):
    local_model = Path(os.getenv('LOCAL_WHISPER_MODEL') or settings.BASE_DIR/'.local/models/ggml-base-q5_1.bin')
    return {'status': 'ok', 'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
            'transcription_configured': bool(((os.getenv('LOCAL_WHISPER_BIN') or shutil.which('whisper-cli')) and local_model.is_file()) or
                                             (os.getenv('AI_ALLOW_BILLABLE','false').lower() == 'true' and os.getenv('HF_TOKEN') and os.getenv('HF_TRANSCRIPTION_MODEL')))}
