"""Groq (OpenAI-compatible API) adapter: TEXT, ASR and VISION, gated by policy and counted in the ledger.

``services/groq.py`` stays the home of the standalone helpers (``generate_metadata``, ``transcribe_clip``) and
of the HTTP client attribute tests patch (``api.services.groq.httpx.post``). This adapter calls through that
module so the same patches apply.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
from pathlib import Path

from ..services import groq as groq_service
from ..services.media import StudioError, run
from . import ledger
from .base import (Billing, Capability, ProviderBase, ProviderError, ProviderInfo, ProviderInvalid,
                   ProviderRateLimited, ProviderUnavailable, schema_of)
from .registry import register

log = logging.getLogger(__name__)
UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024          # free-tier audio upload cap
MAX_IMAGES = 3                                 # qwen/qwen3.8-27b preview limit per request
DEFAULT_VISION_MODEL = 'qwen/qwen3.8-27b'


def _response_json(response):
    try:
        return response.json()
    except Exception:
        return None


def cues_from_verbose_json(payload, duration=None):
    """Same phrase grouping as ``services.groq.transcribe_clip``: words -> <=5-word phrases split on .45 s gaps,
    else segment cues."""
    limit = float('inf') if duration is None else duration
    words = []
    for word in payload.get('words') or []:
        a, b = max(0, float(word['start'])), min(limit, float(word['end']))
        value = word['word'].strip()
        if value and b > a:
            words.append((a, b, value))
    if words:
        cues, group = [], []
        for word in words:
            if group and (len(group) >= 5 or word[0] - group[-1][1] > .45):
                cues.append({'start': group[0][0], 'end': group[-1][1], 'text': ' '.join(i[2] for i in group), 'timing': 'groq_word'})
                group = []
            group.append(word)
        if group:
            cues.append({'start': group[0][0], 'end': group[-1][1], 'text': ' '.join(i[2] for i in group), 'timing': 'groq_word'})
        return cues
    cues = []
    for segment in payload.get('segments') or []:
        a, b = max(0, float(segment['start'])), min(limit, float(segment['end']))
        spoken = segment['text'].strip()
        if spoken and b > a:
            cues.append({'start': a, 'end': b, 'text': spoken, 'timing': 'groq_whisper'})
    return cues


@register
class GroqProvider(ProviderBase):
    info = ProviderInfo(
        name='groq', label='Groq',
        capabilities=frozenset({Capability.TEXT, Capability.ASR, Capability.VISION}),
        billing=Billing.FREE_TIER, required_env=('GROQ_API_KEY',),
        notes=('Free tier is account-dependent; a paid Groq plan bills. Whether the free tier needs a card is '
               'unverified. Strict json_schema output only on openai/gpt-oss-120b, openai/gpt-oss-20b and '
               'qwen/qwen3.8-27b (other models fall back to json_object). Whisper (whisper-large-v3, '
               'whisper-large-v3-turbo) uploads are capped at 25 MB on the free tier. Vision uses the preview model '
               'qwen/qwen3.8-27b, max 3 images per request. Groq TTS is English/Arabic only and is not used. '
               'Disable with GROQ_ENABLED=false or AI_ALLOW_FREE_TIER=false.'))

    def unavailable_reason(self, capability):
        if not os.getenv('GROQ_API_KEY'):
            return 'GROQ_API_KEY is not set.'
        if not groq_service.enabled():   # same switch the standalone helpers honour
            return 'Groq is disabled (GROQ_ENABLED is not "true").'
        return ''

    def model_for(self, capability):
        if capability == Capability.ASR:
            return os.getenv('GROQ_SPEECH_MODEL') or 'whisper-large-v3-turbo'
        if capability == Capability.VISION:
            return os.getenv('GROQ_VISION_MODEL') or DEFAULT_VISION_MODEL
        return os.getenv('GROQ_TEXT_MODEL') or 'openai/gpt-oss-20b'

    # --- HTTP -------------------------------------------------------------------------------------------
    def _post(self, capability, path, *, model, timeout, **kwargs):
        """One guarded request. Returns the parsed JSON body of a 2xx response or raises a ProviderError."""
        reason = self.unavailable_reason(capability)
        if reason:
            raise ProviderUnavailable(reason)
        with ledger.guarded(self, capability, model=model) as rec:
            try:
                response = groq_service.httpx.post(groq_service.API + path, headers=groq_service._headers(),
                                                   timeout=timeout, **kwargs)
            except groq_service.httpx.HTTPError as exc:
                raise ProviderUnavailable(f'Groq request failed ({type(exc).__name__}).', 'ai_timeout') from exc
            status = response.status_code
            rec.http_status = status
            if status == 429:
                raise ProviderRateLimited('Groq rate limit reached; retry later.',
                                          ledger.parse_retry_after(response.headers))
            if status in (401, 403):
                raise ProviderUnavailable('Groq rejected the API key or the model is not enabled for this account.', http_status=status)
            if status == 413:
                raise ProviderUnavailable('Request too large for Groq (audio uploads are capped at 25 MB on the free tier).', http_status=status)
            if status >= 400:
                raise ProviderUnavailable(f'Groq returned HTTP {status}.', http_status=status)
            body = _response_json(response)
            if not isinstance(body, dict):
                raise ProviderInvalid('Groq returned a non-JSON response.', http_status=status)
            usage = body.get('usage') or {}
            if usage.get('total_tokens'):
                rec.units, rec.unit_type = float(usage['total_tokens']), 'tokens'
            return body

    def _chat_json(self, capability, *, task, messages, schema, max_tokens, model):
        strict = {'type': 'json_schema', 'json_schema': {'name': task[:64] or 'result', 'strict': True,
                                                        'schema': schema_of(schema)}}
        response_format = strict if model in groq_service.STRICT_MODELS else {'type': 'json_object'}
        for attempt in range(2):
            try:
                body = self._post(capability, '/chat/completions', model=model, timeout=float(os.getenv('AI_REQUEST_TIMEOUT') or 45),
                                  json={'model': model, 'messages': messages, 'max_completion_tokens': max_tokens,
                                        'response_format': response_format})
            except ProviderUnavailable as exc:
                # Strict schemas Groq cannot compile come back as 400: retry once in json_object mode.
                if exc.http_status == 400 and response_format is strict:
                    response_format = {'type': 'json_object'}
                    continue
                raise
            try:
                return schema.model_validate_json(body['choices'][0]['message']['content'])
            except (KeyError, IndexError, TypeError, ValueError):
                messages = messages + [{'role': 'user', 'content': 'Previous response was invalid. Return only schema-valid JSON.'}]
        raise ProviderInvalid(f'Groq returned invalid structured data for {task}; nothing was invented.')

    # --- TextProvider -----------------------------------------------------------------------------------
    def complete_json(self, *, task, system, user, schema, max_tokens):
        model = self.model_for(Capability.TEXT)
        # The schema is also stated in the prompt so the json_object fallback stays on-shape.
        system_text = system + '\nReturn only JSON matching this schema: ' + json.dumps(schema_of(schema))
        return self._chat_json(Capability.TEXT, task=task, schema=schema, max_tokens=max_tokens, model=model,
                               messages=[{'role': 'system', 'content': system_text}, {'role': 'user', 'content': user}])

    # --- VisionProvider ---------------------------------------------------------------------------------
    def describe(self, frames, schema, *, prompt='Describe these frames as data.', max_tokens=800):
        frames = [Path(f) for f in frames]
        if not frames:
            raise ProviderInvalid('No frames were supplied for vision analysis.')
        if len(frames) > MAX_IMAGES:
            raise ProviderUnavailable(f'Groq vision accepts at most {MAX_IMAGES} images per request.')
        model = self.model_for(Capability.VISION)
        content = [{'type': 'text', 'text': prompt + '\nReturn only JSON matching this schema: ' + json.dumps(schema_of(schema))}]
        for frame in frames:
            mime = mimetypes.guess_type(frame.name)[0] or 'image/jpeg'
            data = base64.b64encode(frame.read_bytes()).decode('ascii')
            content.append({'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{data}'}})
        return self._chat_json(Capability.VISION, task='vision', schema=schema, max_tokens=max_tokens, model=model,
                               messages=[{'role': 'user', 'content': content}])

    # --- ASRProvider ------------------------------------------------------------------------------------
    def transcribe(self, audio, *, language=None, words=False, duration=None):
        audio = Path(audio)
        if audio.stat().st_size > UPLOAD_LIMIT_BYTES:
            raise ProviderUnavailable('Audio exceeds the 25 MB Groq upload cap; split it or use local transcription.')
        model = self.model_for(Capability.ASR)
        data = {'model': model, 'response_format': 'verbose_json',
                'timestamp_granularities[]': ['word', 'segment'] if words else ['segment']}
        if language:
            data['language'] = language
        with audio.open('rb') as stream:
            payload = self._post(Capability.ASR, '/audio/transcriptions', model=model, timeout=40, data=data,
                                 files={'file': (audio.name, stream, mimetypes.guess_type(audio.name)[0] or 'audio/flac')})
        try:
            return cues_from_verbose_json(payload, duration)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderInvalid('Groq returned malformed timestamps.') from exc

    # --- pipeline helpers (keep the old "None on failure" contracts) ------------------------------------
    def transcribe_clip(self, source, folder, start, duration, number):
        """Policy-gated equivalent of ``services.groq.transcribe_clip``; None on any failure (caller falls back)."""
        audio = Path(folder) / f'groq-{number}.flac'
        try:
            run(['ffmpeg', '-nostdin', '-y', '-ss', start, '-i', source, '-t', duration,
                 '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'flac', audio])
            return self.transcribe(audio, words=True, duration=duration) or None
        except (ProviderError, StudioError, OSError) as exc:
            log.warning('Groq clip transcription unavailable: %s', getattr(exc, 'code', type(exc).__name__))
            return None
        finally:
            audio.unlink(missing_ok=True)

    def metadata(self, transcript):
        """Policy-gated, ledger-counted ``services.groq.generate_metadata``; None when blocked or failing.
        The helper still returns None on any failure, but an observer hook lets us see the HTTP status so a 429
        starts the provider cooldown through the ledger like every other Groq call."""
        if not transcript.strip() or self.unavailable_reason(Capability.TEXT):
            return None
        seen = []
        try:
            with ledger.guarded(self, Capability.TEXT, model=self.model_for(Capability.TEXT)) as rec:
                result = groq_service.generate_metadata(transcript, on_response=seen.append)
                status = next((r.status_code for r in reversed(seen) if isinstance(getattr(r, 'status_code', None), int)), None)
                if status is not None:
                    rec.http_status = status
                if status == 429:
                    headers = getattr(seen[-1], 'headers', None)
                    rec.status = 'rate_limited'
                    rec.retry_after_until = ledger.parse_retry_after(headers) or ledger.default_cooldown()
                elif result is None:
                    rec.status = 'error'
                return result
        except ProviderError as exc:
            log.info('Groq metadata skipped: %s', exc.message)
            return None
