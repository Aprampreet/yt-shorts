"""Word timings for narration audio.

Order of preference: provider timestamps, local whisper-cli on the narration (only when configured), otherwise
proportional-by-characters across the whole text, labelled ``timing: 'approximate'`` so captions can say so.
Returns ``{'words': [{'start', 'end', 'text'}], 'timing': 'provider' | 'local_whisper' | 'approximate'}``;
times are monotonic and never exceed ``duration``.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

from ..providers import local_whisper
from ..services import local_transcription
from ..services.media import StudioError, run

_STRONG = ('.', '!', '?', '।', '॥', '…')
_WEAK = (',', ';', ':', '—')


def _tidy(words, duration):
    out, cursor = [], 0.0
    for w in words:
        start = min(max(float(w['start']), cursor), duration)
        end = min(max(float(w['end']), start), duration)
        out.append({'start': round(start, 3), 'end': round(end, 3), 'text': w['text']})
        cursor = start
    return out


def approximate_words(text, duration):
    tokens = (text or '').split()
    if not tokens or duration <= 0:
        return []
    weights = []
    for token in tokens:
        extra = 4 if token.endswith(_STRONG) else 2 if token.endswith(_WEAK) else 0   # room for the spoken pause
        weights.append(len(token) + 1 + extra)
    total, cursor, out = float(sum(weights)), 0.0, []
    for token, weight in zip(tokens, weights):
        span = duration * weight / total
        out.append({'start': cursor, 'end': cursor + span, 'text': token})
        cursor += span
    return out


def _norm(token):
    return re.sub(r'\W+', '', token.lower())


def _whisper_words(audio_path, duration):
    """Per-word cues from whisper-cli, or None when it is not configured or fails."""
    binary, model = local_whisper.binary(), local_whisper.model_path()
    if not binary or not model.is_file():
        return None
    try:
        with tempfile.TemporaryDirectory(prefix='align-') as tmp:
            wav, base = Path(tmp)/'narration.wav', Path(tmp)/'words'
            run(['ffmpeg', '-nostdin', '-y', '-v', 'error', '-i', audio_path, '-vn', '-ac', '1', '-ar', 16000,
                 '-c:a', 'pcm_s16le', wav])
            run([binary, '-ng', '-m', model, '-f', wav, '-l', 'auto', '-oj', '-ml', '1', '-sow', '-of', base, '-np'])
            cues = local_transcription.parse_whisper_json(base.with_suffix('.json'), duration)
    except StudioError:
        return None
    return [{'start': c['start'], 'end': c['end'], 'text': c['text']} for c in cues] or None


def align_words(text, audio_path, duration, provider_words=None):
    duration = float(duration or 0)
    if provider_words:
        words = [{'start': w.start, 'end': w.end, 'text': w.text} if hasattr(w, 'start') else dict(w)
                 for w in provider_words]
        return {'words': _tidy(words, duration), 'timing': 'provider'}
    heard = _whisper_words(audio_path, duration) if audio_path else None
    if heard:
        script = (text or '').split()
        if len(script) == len(heard):          # keep the script's spelling, take whisper's timing
            heard = [dict(h, text=s) for h, s in zip(heard, script)]
        return {'words': _tidy(heard, duration), 'timing': 'local_whisper'}
    return {'words': _tidy(approximate_words(text, duration), duration), 'timing': 'approximate'}
