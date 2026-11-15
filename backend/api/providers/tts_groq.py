"""Groq text-to-speech (Orpheus, preview). English only; off unless GROQ_TTS_MODEL is set.

Checked against https://console.groq.com/docs/text-to-speech and its Orpheus page on 2026-11-15:
``POST https://api.groq.com/openai/v1/audio/speech`` with Bearer auth and ``{model, input, voice, response_format}``;
WAV is the only format; English voices autumn, diana, hannah, austin, daniel, troy; input is limited to 200
characters per request (so text is chunked at sentence boundaries to 200). The Arabic model is not offered here.
Free-tier limits (about 100 requests per day was reported) are account-dependent and unverified.
"""
from __future__ import annotations

import os

from .base import Billing, Capability, ProviderInfo, ProviderInvalid, env_flag
from .registry import register
from .tts_common import TTSBase

API = 'https://api.groq.com/openai/v1/audio/speech'
VOICES = (('autumn', 'Autumn', 'female'), ('diana', 'Diana', 'female'), ('hannah', 'Hannah', 'female'),
          ('austin', 'Austin', 'male'), ('daniel', 'Daniel', 'male'), ('troy', 'Troy', 'male'))


@register
class GroqTTS(TTSBase):
    info = ProviderInfo(
        name='groq_tts', label='Groq TTS (Orpheus, English)', capabilities=frozenset({Capability.TTS}),
        billing=Billing.FREE_TIER, required_env=('GROQ_API_KEY', 'GROQ_TTS_MODEL'), languages=('en',),
        commercial_use=None, rate_limit_rpm=None,
        notes=('English only (the Arabic model is not offered). Preview models; 200 characters per request means '
               'many requests per script and a small daily request allowance on the free tier (unverified). '
               'Commercial-use terms for generated audio were not verified, so assets are marked "unknown" for review. '
               'Speed is applied by ffmpeg.'))
    model_env = 'GROQ_TTS_MODEL'
    max_chars = 200
    fixed_voices = VOICES

    def unavailable_reason(self, capability):
        if not os.getenv('GROQ_API_KEY'):
            return 'GROQ_API_KEY is not set.'
        if not env_flag('GROQ_ENABLED', True):
            return 'Groq is disabled (GROQ_ENABLED=false).'
        model = os.getenv(self.model_env, '')
        if not model:
            return 'GROQ_TTS_MODEL is not set (for example the English Orpheus model from the Groq docs).'
        if 'arabic' in model.lower():
            return 'Only the English Orpheus model is supported by this app; set GROQ_TTS_MODEL to it.'
        return ''

    def request_chunk(self, text, voice_id, speed):
        response = self.post(API, label='Groq TTS', headers={'Authorization': f'Bearer {os.environ["GROQ_API_KEY"]}'},
                             json={'model': self.model_for(Capability.TTS), 'input': text, 'voice': voice_id,
                                   'response_format': 'wav'})
        if not response.content:
            raise ProviderInvalid('Groq TTS returned no audio.', http_status=response.status_code)
        return response.content, 'wav', 1.0
