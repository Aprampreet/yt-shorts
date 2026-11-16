"""Groq (OpenAI-compatible API) adapter: TEXT, ASR and VISION, gated by policy and counted in the ledger.

``services/groq.py`` stays the home of the standalone helpers (``generate_metadata``, ``transcribe_clip``) and
of the HTTP client attribute tests patch (``api.services.groq.httpx.post``). This adapter calls through that
module so the same patches apply.
"""
from __future__ import annotations

import base64
import json
import logging
import math
import mimetypes
import os
import re
import time
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from ..services import groq as groq_service
from ..services.media import StudioError, run
from . import ledger
from .base import (Billing, Capability, ProviderBase, ProviderError, ProviderInfo, ProviderInvalid,
                   ProviderRateLimited, ProviderUnavailable, env_int, schema_of, strict_schema)
from .registry import register

log = logging.getLogger(__name__)
UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024          # free-tier audio upload cap
MAX_IMAGES = 3                                 # qwen/qwen3.8-27b preview limit per request
DEFAULT_VISION_MODEL = 'qwen/qwen3.8-27b'
# Reasoning models whose hidden reasoning tokens count against max_completion_tokens. Groq's reasoning guide
# (checked 2026-11-15) lists reasoning_effort low|medium|high and include_reasoning for GPT-OSS; reasoning_format
# is not supported on GPT-OSS. Qwen 3.8 is left alone here (its effort values differ and were not verified live).
REASONING_PREFIXES = ('openai/gpt-oss-',)
REASONING_EFFORTS = ('low', 'medium', 'high')
RATE_LIMIT_MARGIN = 1.0                        # seconds added to Groq's "try again in" before the single retry
DETAIL_LIMIT = 300
TOKEN_CAP = re.compile(r'max(?:imum)?[ _]?(?:completion[ _])?tokens', re.I)
_SECRET = re.compile(r'(?i)\bbearer\s+\S+|\bgsk_[A-Za-z0-9]{6,}|\bsk-[A-Za-z0-9_-]{6,}|'
                     r'\b(?:api[_-]?key|authorization|token)\s*[=:]\s*\S+')
_TRY_AGAIN = re.compile(r'try again in\s+((?:\d+(?:\.\d+)?(?:ms|h|m|s))+)', re.I)
_STRICT_REJECTED = set()                       # schema names whose strict rejection was already logged


def _response_json(response):
    try:
        return response.json()
    except Exception:
        return None


def sanitize(text, limit=DETAIL_LIMIT):
    """One line of provider text that is safe to show and log: no key, bearer token or auth header values."""
    text = ' '.join(str(text or '').split())
    key = os.getenv('GROQ_API_KEY')
    if key:
        text = text.replace(key, '[redacted]')
    text = _SECRET.sub('[redacted]', text)
    return text if len(text) <= limit else text[:limit - 3].rstrip() + '...'


def groq_error(payload):
    """(code, message, failed_generation) from an OpenAI-style error body, sanitised; '' where absent."""
    error = payload.get('error') if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return '', '', ''
    code = sanitize(error.get('code') or error.get('type') or '', 60)
    return code, sanitize(error.get('message') or ''), sanitize(error.get('failed_generation') or '', 160)


def retry_seconds(headers, message=''):
    """Seconds Groq asks us to wait: Retry-After / x-ratelimit-reset-* headers, else "try again in 17.61s"."""
    now = timezone.now()
    until = ledger.parse_retry_after(headers, now)
    if until is not None:
        return max(0.0, (until - now).total_seconds())
    match = _TRY_AGAIN.search(message or '')
    seconds = ledger._seconds(match.group(1)) if match else None
    return seconds if seconds and seconds > 0 else None


def is_reasoning_model(model):
    return bool(model) and model.startswith(REASONING_PREFIXES)


def reasoning_effort():
    value = (os.getenv('GROQ_REASONING_EFFORT') or 'low').strip().lower()
    return value if value in REASONING_EFFORTS else 'low'


def max_rate_limit_wait():
    """Longest 429 wait absorbed inside one call (GROQ_RATE_LIMIT_MAX_WAIT, default 25 s, 0 disables, max 60)."""
    return float(min(max(env_int('GROQ_RATE_LIMIT_MAX_WAIT', 25), 0), 60))


def headroom(tokens, messages):
    """A modestly larger completion cap (x1.5) that keeps input + output under GROQ_REQUEST_TOKEN_BUDGET (default
    6500, below the 8,000 tokens/minute free tier). Input is estimated at ~3 characters per token (conservative)."""
    budget = env_int('GROQ_REQUEST_TOKEN_BUDGET', 6500)
    estimate = len(json.dumps(messages, ensure_ascii=False)) // 3
    return max(tokens, min(int(tokens * 1.5), budget - estimate))


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
    def _post(self, capability, path, *, model, timeout, absorb_wait=0.0, schema_attempt=False, **kwargs):
        """One guarded request. Returns the parsed JSON body of a 2xx response or raises a ProviderError.

        Error messages carry Groq's own ``error.code`` / ``error.message`` (sanitised, truncated). A 429 whose
        wait is <= ``absorb_wait`` seconds is raised with ``wait_seconds`` set and a cooldown that ends now, so
        the caller can sleep and retry once without blocking other calls; longer waits start the normal cooldown.
        ``schema_attempt`` marks a strict json_schema request whose 400 the caller handles (and logs) itself."""
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
            if status >= 400:
                code, detail, failed = groq_error(_response_json(response))
                suffix = (f' ({code})' if code else '') + (f': {detail.rstrip(". ")}' if detail else '')
                if failed:
                    suffix += f' [{failed.rstrip(". ")}]'
                quiet = schema_attempt and status == 400 and code != 'json_validate_failed'
                (log.debug if quiet else log.warning)('Groq HTTP %s for %s%s', status, model, suffix)
            if status == 429:
                seconds = retry_seconds(response.headers, detail)
                wait = f' Wait about {math.ceil(seconds)} s and retry.' if seconds else ' Retry later.'
                if absorb_wait and seconds is not None and seconds <= absorb_wait:
                    exc = ProviderRateLimited(f'Groq rate limit reached{suffix}.{wait}', timezone.now())
                    exc.wait_seconds = seconds
                    raise exc
                until = timezone.now() + timedelta(seconds=seconds) if seconds else None
                raise ProviderRateLimited(f'Groq rate limit reached{suffix}.{wait}', until)
            if status in (401, 403):
                raise ProviderUnavailable('Groq rejected the API key or the model is not enabled for this account'
                                          f'{suffix}.', http_status=status)
            if status == 413:
                raise ProviderUnavailable('Request too large for Groq (audio uploads are capped at 25 MB on the free '
                                          f'tier){suffix}.', http_status=status)
            if status >= 400:
                cls = ProviderInvalid if code == 'json_validate_failed' else ProviderUnavailable
                exc = cls(f'Groq returned HTTP {status}{suffix}.', http_status=status)
                exc.groq_code, exc.groq_failed = code, failed
                raise exc
            body = _response_json(response)
            if not isinstance(body, dict):
                raise ProviderInvalid('Groq returned a non-JSON response.', http_status=status)
            usage = body.get('usage') or {}
            if usage.get('total_tokens'):
                rec.units, rec.unit_type = float(usage['total_tokens']), 'tokens'
            return body

    def _wait_for_rate_limit(self, seconds):
        """Sleep through a short tokens-per-minute window, then stop if the job was cancelled meanwhile."""
        delay = seconds + RATE_LIMIT_MARGIN
        log.warning('Groq rate limited; waiting %.1f s and retrying once.', delay)
        time.sleep(delay)
        job = getattr(self, 'job', None)
        if job is not None and getattr(job, 'pk', None) is not None:
            from ..models import GenerationJob
            from ..pipeline.context import Cancelled
            if GenerationJob.objects.filter(pk=job.pk, status='cancelled').exists():
                raise Cancelled()

    def _chat_json(self, capability, *, task, messages, schema, max_tokens, model):
        """Structured chat call with at most one retry of each kind: strict schema rejected -> json_object;
        ``json_validate_failed`` (e.g. reasoning used up the token cap) -> low effort + bounded headroom; short
        429 -> wait and retry; app-side validation failure -> ask again. Nothing loops."""
        name = task[:64] or 'result'
        strict = {'type': 'json_schema', 'json_schema': {'name': name, 'strict': True, 'schema': strict_schema(schema)}}
        response_format = strict if model in groq_service.STRICT_MODELS else {'type': 'json_object'}
        effort = reasoning_effort() if is_reasoning_model(model) else None
        tokens, spent = max_tokens, set()
        timeout = float(os.getenv('AI_REQUEST_TIMEOUT') or 45)
        while True:
            payload = {'model': model, 'messages': messages, 'max_completion_tokens': tokens,
                       'response_format': response_format}
            if effort:
                payload['reasoning_effort'] = effort
                payload['include_reasoning'] = False      # gpt-oss: reasoning text is not returned
            try:
                body = self._post(capability, '/chat/completions', model=model, timeout=timeout, json=payload,
                                  absorb_wait=0.0 if 'rate' in spent else max_rate_limit_wait(),
                                  schema_attempt=response_format is strict)
            except ProviderRateLimited as exc:
                if getattr(exc, 'wait_seconds', None) is None:
                    raise
                spent.add('rate')
                self._wait_for_rate_limit(exc.wait_seconds)
                continue
            except ProviderError as exc:
                if exc.http_status == 400 and getattr(exc, 'groq_code', '') == 'json_validate_failed':
                    if 'generation' in spent:
                        raise ProviderInvalid(f'{exc.message} The {task} step was retried once; nothing was '
                                              'invented.', http_status=400) from exc
                    spent.add('generation')
                    if effort:
                        effort = 'low'
                    if TOKEN_CAP.search(getattr(exc, 'groq_failed', '') or ''):
                        tokens = headroom(tokens, messages)
                    continue
                if exc.http_status == 400 and response_format is strict:
                    # Strict schemas Groq cannot compile come back as 400: retry once in json_object mode.
                    if name not in _STRICT_REJECTED:
                        _STRICT_REJECTED.add(name)
                        log.warning('Groq rejected the strict schema for %s on %s; using json_object instead. %s',
                                    name, model, exc.message)
                    response_format = {'type': 'json_object'}
                    continue
                raise
            try:
                return schema.model_validate_json(body['choices'][0]['message']['content'])
            except (KeyError, IndexError, TypeError, ValueError):
                if 'invalid' in spent:
                    break
                spent.add('invalid')
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
