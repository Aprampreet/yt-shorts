import os
import shutil
import uuid
from django.middleware.csrf import get_token
from ninja import Router

router = Router()


@router.get('/v1/session', auth=None)
def session(request):
    if 'studio_owner' not in request.session:
        request.session['studio_owner'] = str(uuid.uuid4())
    return {'csrf_token': get_token(request)}


def _provider_view():
    """Registry snapshot plus a usable(name, capability) lookup (no network)."""
    from ..providers import registry
    snapshot = registry.status()
    by_name = {entry['name']: entry for entry in snapshot['providers']}

    def usable(name, capability):
        return bool(by_name.get(name, {}).get('capability_status', {}).get(capability, {}).get('usable'))
    return snapshot, by_name, usable


@router.get('/v1/capabilities', auth=None)
def capabilities(request):
    from ..providers import policy
    from ..providers.base import paid_allowed
    from ..providers.huggingface import HuggingFaceProvider
    snapshot, by_name, usable = _provider_view()
    approved = paid_allowed()    # AI_ALLOW_PAID_PROVIDERS, or the legacy AI_ALLOW_BILLABLE
    token = bool(os.getenv('HF_TOKEN'))
    local_asr = usable('local_whisper', 'asr')
    groq = by_name.get('groq', {})
    # Existing keys keep their meaning: Hugging Face hosted features plus local whisper for source ASR.
    return {'hosted_inference_enabled':token and policy.check(HuggingFaceProvider.info).allowed,
        'asr':local_asr or usable('huggingface', 'asr'),
        'translation':usable('huggingface', 'translation'),
        'original_analysis':usable('huggingface', 'text'),
        'groq':{'available':bool(groq.get('available')), 'billing':groq.get('billing'),
                'text':usable('groq', 'text'), 'asr':usable('groq', 'asr'), 'vision':usable('groq', 'vision'),
                'reason':groq.get('reason', 'Groq adapter is not registered.')},
        'providers':snapshot['providers'],
        'youtube_oauth_configured':bool(os.getenv('YOUTUBE_CLIENT_ID') and os.getenv('YOUTUBE_CLIENT_SECRET') and os.getenv('SOCIAL_ENCRYPTION_KEY')),
        'instagram_publishing_available':False,
        'reason':('Local audio transcription is available; hosted analysis and translation require explicit billing approval.'
                  if local_asr and not approved else
                  'Install whisper-cli and a multilingual model for offline captions.' if not local_asr and not approved else '')}


@router.get('/v1/health', auth=None)
def health(request):
    from ..providers import registry
    # Env/policy switches only (use_db=False): health stays cheap and independent of the ledger.
    configured = any(registry.evaluate(registry.get(name), 'asr', use_db=False)['usable']
                     for name in ('local_whisper', 'huggingface') if name in registry.PROVIDERS)
    return {'status': 'ok', 'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
            'transcription_configured': configured}
