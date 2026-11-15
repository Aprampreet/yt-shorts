"""Google Gemini API (AI Studio key) adapter: TEXT and VISION via REST ``models.generateContent``.

Request shape checked against the official API reference on 2026-11-15: ``POST /v1beta/models/{model}:generateContent``
with ``x-goog-api-key``; ``generationConfig.responseMimeType='application/json'``, ``responseJsonSchema`` and
``maxOutputTokens``; image parts as ``inlineData {mimeType, data}``; text at ``candidates[].content.parts[].text``;
``usageMetadata.totalTokenCount``. ``systemInstruction`` (``{parts:[{text}]}``) is the long-standing
generateContent field but was not shown on the fetched reference page. The docs now recommend the newer Interactions API but state that
generateContent remains fully supported. The structured-output guide says length keywords (minLength/maxLength
etc.) are stripped without error, so every response is validated app-side with the Pydantic schema.

Model IDs are never hardcoded: set GEMINI_TEXT_MODEL / GEMINI_VISION_MODEL to a model your key can use.
TTS is intentionally not implemented here.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path

import httpx

from . import ledger
from .base import (Billing, Capability, ProviderBase, ProviderInfo, ProviderInvalid, ProviderRateLimited,
                   ProviderUnavailable, env_flag, schema_of)
from .registry import register

API = 'https://generativelanguage.googleapis.com/v1beta'
MAX_INLINE_BYTES = 15 * 1024 * 1024     # 20 MB request cap includes base64 inflation (~4/3) and the prompt
MODEL_ENV = {Capability.TEXT: 'GEMINI_TEXT_MODEL', Capability.VISION: 'GEMINI_VISION_MODEL'}


@register
class GeminiProvider(ProviderBase):
    info = ProviderInfo(
        name='gemini', label='Google Gemini API',
        capabilities=frozenset({Capability.TEXT, Capability.VISION}),
        billing=Billing.FREE_TIER, required_env=('GEMINI_API_KEY', 'GEMINI_TEXT_MODEL | GEMINI_VISION_MODEL'),
        notes=('Free tier covers Flash / Flash-Lite text and vision models; per-model free limits are unverified, and '
               'a key on a billing-enabled project is billed. Free-tier caveats: prompts and responses may be used to '
               'improve Google products and may be read by human reviewers, so do not send private material; the free '
               'tier is not permitted for serving end users in the EEA, the UK or Switzerland. JSON-schema output '
               'supports a schema subset, so results are validated app-side. Disable with GEMINI_ENABLED=false or '
               'AI_ALLOW_FREE_TIER=false.'))

    def unavailable_reason(self, capability):
        if not os.getenv('GEMINI_API_KEY'):
            return 'GEMINI_API_KEY is not set.'
        if not env_flag('GEMINI_ENABLED', True):
            return 'Gemini is disabled (GEMINI_ENABLED=false).'
        if capability in MODEL_ENV and not os.getenv(MODEL_ENV[capability]):
            return (f'{MODEL_ENV[capability]} is not set; choose a free-tier Flash or Flash-Lite model ID from the '
                    'Gemini API model list.')
        return ''

    def model_for(self, capability):
        return os.getenv(MODEL_ENV.get(capability, ''), '') if capability in MODEL_ENV else ''

    def _generate(self, capability, *, task, system, parts, schema, max_tokens):
        reason = self.unavailable_reason(capability)
        if reason:
            raise ProviderUnavailable(reason)
        model = self.model_for(capability)
        body = {'contents': [{'role': 'user', 'parts': parts}],
                'generationConfig': {'responseMimeType': 'application/json', 'responseJsonSchema': schema_of(schema),
                                     'maxOutputTokens': max_tokens}}
        if system:
            body['systemInstruction'] = {'parts': [{'text': system}]}
        for _ in range(2):
            with ledger.guarded(self, capability, model=model) as rec:
                try:
                    response = httpx.post(f'{API}/models/{model}:generateContent', json=body,
                                          headers={'x-goog-api-key': os.environ['GEMINI_API_KEY']},
                                          timeout=float(os.getenv('AI_REQUEST_TIMEOUT') or 45))
                except httpx.HTTPError as exc:
                    raise ProviderUnavailable(f'Gemini request failed ({type(exc).__name__}).', 'ai_timeout') from exc
                rec.http_status = status = response.status_code
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if status == 429:
                    raise ProviderRateLimited('Gemini free-tier rate limit reached; retry later.',
                                              ledger.parse_retry_after(response.headers, body=payload))
                if status in (401, 403):
                    raise ProviderUnavailable('Gemini rejected the API key, or the model is not available to it.', http_status=status)
                if status >= 400:
                    message = ((payload or {}).get('error') or {}).get('message', '') if isinstance(payload, dict) else ''
                    raise ProviderUnavailable(f'Gemini returned HTTP {status}. {message}'.strip(), http_status=status)
                if not isinstance(payload, dict):
                    raise ProviderInvalid('Gemini returned a non-JSON response.', http_status=status)
                tokens = (payload.get('usageMetadata') or {}).get('totalTokenCount')
                if tokens:
                    rec.units, rec.unit_type = float(tokens), 'tokens'
                candidates = payload.get('candidates') or []
                if not candidates:
                    blocked = (payload.get('promptFeedback') or {}).get('blockReason', 'no candidates')
                    raise ProviderInvalid(f'Gemini returned no answer for {task} ({blocked}).', http_status=status)
                text = ''.join(part.get('text', '') for part in (candidates[0].get('content') or {}).get('parts') or [])
            try:
                return schema.model_validate_json(text)
            except (ValueError, TypeError):
                body['contents'].append({'role': 'model', 'parts': [{'text': text[:4000]}]})
                body['contents'].append({'role': 'user', 'parts': [{'text': 'That was not valid for the schema. Return only schema-valid JSON.'}]})
        raise ProviderInvalid(f'Gemini returned invalid structured data for {task}; nothing was invented.')

    def complete_json(self, *, task, system, user, schema, max_tokens):
        return self._generate(Capability.TEXT, task=task, system=system, parts=[{'text': user}],
                              schema=schema, max_tokens=max_tokens)

    def describe(self, frames, schema, *, prompt='Describe these frames as data.', max_tokens=800):
        frames = [Path(f) for f in frames]
        if not frames:
            raise ProviderInvalid('No frames were supplied for vision analysis.')
        if sum(f.stat().st_size for f in frames) > MAX_INLINE_BYTES:
            raise ProviderUnavailable('Frames exceed the inline request size limit; send fewer or smaller images.')
        parts = [{'text': prompt + '\nReturn only JSON matching this schema: ' + json.dumps(schema_of(schema))}]
        for frame in frames:
            parts.append({'inlineData': {'mimeType': mimetypes.guess_type(frame.name)[0] or 'image/jpeg',
                                         'data': base64.b64encode(frame.read_bytes()).decode('ascii')}})
        return self._generate(Capability.VISION, task='vision', system='', parts=parts, schema=schema, max_tokens=max_tokens)
