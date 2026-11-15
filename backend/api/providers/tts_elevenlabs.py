"""ElevenLabs text-to-speech: PAID, opt-in only.

Checked against https://elevenlabs.io/docs/api-reference/text-to-speech/convert on 2026-11-15:
``POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000`` with ``xi-api-key``; body
``{text, model_id, voice_settings:{speed}}`` (default model ``eleven_multilingual_v2``; the page states no speed range and
no per-request length limit). Voices list: ``GET /v2/voices`` (not called here; configure ELEVENLABS_VOICES as
``id:Label,id:Label`` or type a voice id). The free plan has no commercial licence, so ELEVENLABS_PLAN_COMMERCIAL
(default false) decides whether generated assets are labelled restricted. Blocked unless ELEVENLABS_API_KEY,
AI_ALLOW_PAID_PROVIDERS=true and a positive AI_DAILY_BUDGET_USD are all set. Voice cloning is never used.
"""
from __future__ import annotations

import math
import os
import re
from decimal import Decimal

from .base import Billing, Capability, Estimate, ProviderInfo, ProviderInvalid, Voice, env_flag
from .registry import register
from .tts_common import TTSBase, clamp_speed

API = 'https://api.elevenlabs.io/v1/text-to-speech'
USD_PER_1000_CHARS = Decimal('0.30')     # deliberately conservative upper-ish estimate for the budget gate
VOICE_ID = re.compile(r'^[A-Za-z0-9]{10,40}$')


@register
class ElevenLabsTTS(TTSBase):
    info = ProviderInfo(
        name='elevenlabs', label='ElevenLabs', capabilities=frozenset({Capability.TTS}), billing=Billing.PAID,
        required_env=('ELEVENLABS_API_KEY', 'AI_ALLOW_PAID_PROVIDERS=true', 'AI_DAILY_BUDGET_USD>0'),
        commercial_use=None,
        notes=('Paid, opt-in. The free ElevenLabs plan has no commercial licence: set ELEVENLABS_PLAN_COMMERCIAL=true '
               'only if your plan includes one, otherwise generated audio is labelled restricted for monetised use. '
               'Multilingual model; Hindi/Hinglish quality is unverified. Speed 0.8-1.2 is applied by the provider, the '
               'rest by ffmpeg. Cost estimate is a conservative flat rate per character.'))
    model_env = 'ELEVENLABS_MODEL'
    max_chars = 2000

    def commercial_use(self):
        return env_flag('ELEVENLABS_PLAN_COMMERCIAL', False)

    def unavailable_reason(self, capability):
        if not os.getenv('ELEVENLABS_API_KEY'):
            return 'ELEVENLABS_API_KEY is not set (paid provider, opt-in only).'
        return ''

    def model_for(self, capability):
        return os.getenv('ELEVENLABS_MODEL') or 'eleven_multilingual_v2'

    def voices(self, language):
        out = []
        for entry in (os.getenv('ELEVENLABS_VOICES') or '').split(','):
            ident, _, label = entry.strip().partition(':')
            if ident.strip():
                out.append(Voice(id=ident.strip(), name=(label or ident).strip(), language='multilingual'))
        return out

    def check_voice(self, voice_id):
        if not VOICE_ID.match(voice_id or ''):
            from ..services.media import StudioError
            raise StudioError('invalid_voice', 'Enter a valid ElevenLabs voice id.')

    def estimate(self, text):
        n = len(text or '')
        return Estimate(units=n, unit_type='characters',
                        usd=(USD_PER_1000_CHARS * max(n, 1) / 1000).quantize(Decimal('0.0001')))

    def request_chunk(self, text, voice_id, speed):
        applied = clamp_speed(speed, 0.8, 1.2)
        body = {'text': text, 'model_id': self.model_for(Capability.TTS)}
        if abs(applied - 1.0) > 0.005:
            body['voice_settings'] = {'speed': applied}
        response = self.post(f'{API}/{voice_id}', label='ElevenLabs', params={'output_format': 'pcm_24000'},
                             headers={'xi-api-key': os.environ['ELEVENLABS_API_KEY']}, json=body)
        if not response.content:
            raise ProviderInvalid('ElevenLabs returned no audio.', http_status=response.status_code)
        return response.content, 'pcm', applied
