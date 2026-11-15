"""Gemini API text-to-speech (AI Studio key). The PRIMARY free-tier narration provider.

Request shape checked against https://aistudio.google.com/docs/speech-generation on 2026-11-15: the guide uses the
Interactions API, ``POST /v1beta/interactions`` with ``x-goog-api-key``; body ``{model, input:[{type:'user_input',
content:[{type:'text', text}]}], response_format:{type:'audio'}, generation_config:{speech_config:[{voice}]}}``.
The audio is at ``steps[type=model_output].content[type=audio].data`` (base64); unary responses are RIFF WAV,
24 kHz mono 16-bit (headerless PCM is accepted too). 30 prebuilt voices; language is detected automatically and
Hindi is listed for the Flash TTS models. The model id is never hardcoded: set GEMINI_TTS_MODEL to a TTS model your
key can use. Input length limits were not documented, so text is sent in chunks of at most 1500 characters.
"""
from __future__ import annotations

import base64
import os

from .base import Billing, Capability, ProviderInfo, ProviderInvalid, env_flag
from .registry import register
from .tts_common import TTSBase

API = 'https://generativelanguage.googleapis.com/v1beta'
VOICES = tuple((name, name, None) for name in (
    'Zephyr', 'Puck', 'Charon', 'Kore', 'Fenrir', 'Leda', 'Orus', 'Aoede', 'Callirrhoe', 'Autonoe', 'Enceladus',
    'Iapetus', 'Umbriel', 'Algieba', 'Despina', 'Erinome', 'Algenib', 'Rasalgethi', 'Laomedeia', 'Achernar',
    'Alnilam', 'Schedar', 'Gacrux', 'Pulcherrima', 'Achird', 'Zubenelgenubi', 'Vindemiatrix', 'Sadachbia',
    'Sadaltager', 'Sulafat'))


@register
class GeminiTTS(TTSBase):
    info = ProviderInfo(
        name='gemini_tts', label='Google Gemini TTS', capabilities=frozenset({Capability.TTS}),
        billing=Billing.FREE_TIER, required_env=('GEMINI_API_KEY', 'GEMINI_TTS_MODEL'), commercial_use=True,
        notes=('Free tier: prompts and audio may be used to improve Google products and may be read by human '
               'reviewers, so do not narrate private material. The free tier is not permitted for serving users in '
               'the EEA, the UK or Switzerland. Per-model free limits are unverified. Language is auto-detected; '
               'Hindi is listed for the Flash TTS models but Hinglish (Hindi in Latin script) quality is unverified. '
               'Speed is applied by ffmpeg. Google does not claim ownership of generated output.'))
    model_env = 'GEMINI_TTS_MODEL'
    max_chars = 1500
    fixed_voices = VOICES

    def unavailable_reason(self, capability):
        if not os.getenv('GEMINI_API_KEY'):
            return 'GEMINI_API_KEY is not set.'
        if not env_flag('GEMINI_ENABLED', True):
            return 'Gemini is disabled (GEMINI_ENABLED=false).'
        if not os.getenv(self.model_env):
            return 'GEMINI_TTS_MODEL is not set; choose a Gemini TTS model ID from the Gemini API model list.'
        return ''

    def request_chunk(self, text, voice_id, speed):
        body = {'model': self.model_for(Capability.TTS),
                'input': [{'type': 'user_input', 'content': [{'type': 'text', 'text': text}]}],
                'response_format': {'type': 'audio'},
                'generation_config': {'speech_config': [{'voice': voice_id}]}}
        response = self.post(f'{API}/interactions', label='Gemini TTS', json=body,
                             headers={'x-goog-api-key': os.environ['GEMINI_API_KEY']})
        try:
            payload = response.json()
        except ValueError:
            raise ProviderInvalid('Gemini TTS returned a non-JSON response.', http_status=response.status_code)
        data = None
        for step in (payload.get('steps') or []) if isinstance(payload, dict) else []:
            if isinstance(step, dict) and step.get('type') == 'model_output':
                for item in step.get('content') or []:
                    if isinstance(item, dict) and item.get('type') == 'audio' and item.get('data'):
                        data = item['data']
        if not data:
            raise ProviderInvalid('Gemini TTS returned no audio.', http_status=response.status_code)
        try:
            raw = base64.b64decode(data, validate=False)
        except ValueError:
            raise ProviderInvalid('Gemini TTS returned undecodable audio.')
        return raw, ('wav' if raw[:4] == b'RIFF' else 'pcm'), 1.0
