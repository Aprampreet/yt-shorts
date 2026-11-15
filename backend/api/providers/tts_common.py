"""Shared plumbing for hosted TTS adapters: sentence chunking, HTTP error mapping and ffmpeg assembly.

Adapters implement ``request_chunk`` (one HTTP request for one short chunk). ``synthesize`` does the policy
pre-check for the whole text, one ledger row per request (units = characters), then concatenates the chunks
(with a short pause between them), applies any remaining speed change with ``atempo`` and loudness-normalises
to a mono 24 kHz WAV. Pauses inside the speech (punctuation) come from the provider and are never trimmed.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

import httpx

from ..services.assets import probe_av
from ..services.media import StudioError, run
from . import ledger, policy
from .base import (Capability, Estimate, ProviderBase, ProviderInvalid, ProviderRateLimited, ProviderUnavailable,
                   Synthesis, Voice)

MIN_SPEED, MAX_SPEED = 0.8, 1.25
SAMPLE_RATE = 24000
PAUSE_SECONDS = 0.18
_SENTENCE = re.compile(r'(?<=[.!?।॥…])\s+|\n+')
_CLAUSE = re.compile(r'(?<=[,;:—])\s+')


def chunk_text(text: str, limit: int) -> list[str]:
    """Pack whole sentences into chunks of at most ``limit`` characters (long sentences split at clauses, then words)."""
    text = ' '.join((text or '').split()) if '\n' not in (text or '') else (text or '').strip()
    pieces = []
    for sentence in (s.strip() for s in _SENTENCE.split(text)):
        if not sentence:
            continue
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        for clause in _CLAUSE.split(sentence):
            while len(clause) > limit:
                cut = clause.rfind(' ', 0, limit)
                cut = cut if cut > 0 else limit
                pieces.append(clause[:cut].strip())
                clause = clause[cut:].strip()
            if clause:
                pieces.append(clause)
    chunks, current = [], ''
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > limit:
            chunks.append(current)
            current = piece
        else:
            current = f'{current} {piece}'.strip()
    if current:
        chunks.append(current)
    return chunks


def clamp_speed(speed, low=MIN_SPEED, high=MAX_SPEED):
    return max(low, min(high, float(speed)))


class TTSBase(ProviderBase):
    model_env = ''
    max_chars = 1000
    fixed_voices: tuple = ()          # ((id, label, gender|None), ...); empty means any id is accepted

    # --- metadata -------------------------------------------------------------------------------------
    def commercial_use(self):
        return self.info.commercial_use

    def model_for(self, capability):
        return os.getenv(self.model_env, '') if self.model_env else ''

    def voices(self, language):
        return [Voice(id=v[0], name=v[1], language='multilingual' if not self.info.languages else self.info.languages[0])
                for v in self.fixed_voices]

    def voice_meta(self, language):
        genders = {v[0]: v[2] for v in self.fixed_voices}
        return [{'id': v.id, 'label': v.name, 'language': v.language, **({'gender': genders[v.id]} if genders.get(v.id) else {})}
                for v in self.voices(language)]

    def estimate(self, text):
        return Estimate(units=len(text or ''), unit_type='characters', usd=Decimal(0))

    # --- adapter hooks --------------------------------------------------------------------------------
    def request_chunk(self, text: str, voice_id: str, speed: float):
        """One request. Returns (audio_bytes, fmt, applied_speed); fmt is 'wav' or 'pcm' (s16le 24 kHz mono)."""
        raise NotImplementedError

    def check_voice(self, voice_id):
        if not voice_id or not str(voice_id).strip():
            raise StudioError('invalid_voice', 'Choose a voice.')
        if self.fixed_voices and voice_id not in {v[0] for v in self.fixed_voices}:
            raise StudioError('invalid_voice', f'"{voice_id}" is not a {self.info.label} voice.')

    # --- helpers --------------------------------------------------------------------------------------
    @staticmethod
    def timeout():
        return float(os.getenv('AI_REQUEST_TIMEOUT') or 45)

    def post(self, url, *, label, **kwargs):
        try:
            response = httpx.post(url, timeout=self.timeout(), **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f'{label} request failed ({type(exc).__name__}).', 'ai_timeout') from exc
        status = response.status_code
        if status < 400:
            return response
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if status == 429:
            raise ProviderRateLimited(f'{label} rate limit reached; retry later.',
                                      ledger.parse_retry_after(response.headers, body=payload))
        if status in (401, 403):
            raise ProviderUnavailable(f'{label} rejected the API key, or the model or voice is not available to it.',
                                      http_status=status)
        message = ''
        if isinstance(payload, dict):
            err = payload.get('error')
            message = (err.get('message', '') if isinstance(err, dict) else str(err or ''))[:200]
        raise ProviderUnavailable(f'{label} returned HTTP {status}. {message}'.strip(), http_status=status)

    # --- synthesis ------------------------------------------------------------------------------------
    def synthesize(self, text, *, voice_id, speed, out):
        text = (text or '').strip()
        if not text:
            raise StudioError('empty_text', 'There is no text to narrate.')
        if not MIN_SPEED <= float(speed) <= MAX_SPEED:
            raise StudioError('invalid_speed', f'Speed must be between {MIN_SPEED} and {MAX_SPEED}.')
        reason = self.unavailable_reason(Capability.TTS)
        if reason:
            raise ProviderUnavailable(reason)
        self.check_voice(voice_id)
        policy.check(self.info, self.estimate(text), self.job, use_db=self.record).raise_for_block()
        chunks = chunk_text(text, self.max_chars)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix='tts-', dir=out.parent))
        try:
            parts, applied = [], 1.0
            for i, chunk in enumerate(chunks):
                with ledger.guarded(self, Capability.TTS, model=self.model_for(Capability.TTS),
                                    estimate=self.estimate(chunk), units=len(chunk), unit_type='characters'):
                    data, fmt, applied = self.request_chunk(chunk, voice_id, float(speed))
                if not data:
                    raise ProviderInvalid(f'{self.info.label} returned no audio.')
                raw = tmp/f'raw{i}.{"pcm" if fmt == "pcm" else "wav"}'
                raw.write_bytes(data)
                norm = tmp/f'part{i}.wav'
                src = ['-f', 's16le', '-ar', str(SAMPLE_RATE), '-ac', '1'] if fmt == 'pcm' else []
                try:
                    run(['ffmpeg', '-nostdin', '-y', '-v', 'error', *src, '-i', raw, '-ac', '1', '-ar', SAMPLE_RATE,
                         '-c:a', 'pcm_s16le', norm])
                except StudioError as exc:
                    raise ProviderInvalid(f'{self.info.label} returned audio that could not be decoded.') from exc
                parts.append(norm)
            silence = tmp/'pause.wav'
            run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'anullsrc=r={SAMPLE_RATE}:cl=mono',
                 '-t', PAUSE_SECONDS, '-c:a', 'pcm_s16le', silence])
            listing = tmp/'list.txt'
            lines = []
            for i, part in enumerate(parts):
                if i:
                    lines.append(f"file '{silence}'")
                lines.append(f"file '{part}'")
            listing.write_text('\n'.join(lines) + '\n')
            remaining = float(speed) / (applied or 1.0)
            filters = []
            if abs(remaining - 1.0) > 0.005:
                filters.append(f'atempo={remaining:.4f}')
            filters.append('loudnorm=I=-16:TP=-1.5:LRA=11')
            tmp_out = tmp/'final.wav'
            run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-f', 'concat', '-safe', '0', '-i', listing,
                 '-af', ','.join(filters), '-ac', '1', '-ar', SAMPLE_RATE, '-c:a', 'pcm_s16le', tmp_out])
            shutil.move(str(tmp_out), out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        duration = probe_av(out, 'audio')['duration']
        return Synthesis(path=out, duration=duration, words=None, characters=len(text))
