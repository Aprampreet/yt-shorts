"""Offline ASR through whisper-cli (LOCAL, never billed). Wraps ``services.local_transcription``."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from django.conf import settings

from ..services import local_transcription
from . import ledger
from .base import Billing, Capability, ProviderBase, ProviderInfo
from .registry import register


def model_path() -> Path:
    return Path(os.getenv('LOCAL_WHISPER_MODEL') or settings.BASE_DIR / '.local' / 'models' / 'ggml-base-q5_1.bin')


def binary():
    return os.getenv('LOCAL_WHISPER_BIN') or shutil.which('whisper-cli')


@register
class LocalWhisperASR(ProviderBase):
    info = ProviderInfo(
        name='local_whisper', label='Local Whisper (whisper-cli)', capabilities=frozenset({Capability.ASR}),
        billing=Billing.LOCAL, required_env=('LOCAL_WHISPER_BIN or whisper-cli on PATH', 'LOCAL_WHISPER_MODEL'),
        commercial_use=True,
        notes='Runs on this machine; language is auto-detected. Accuracy depends on the model file you install.')

    def unavailable_reason(self, capability):
        if not binary():
            return 'whisper-cli is not installed (set LOCAL_WHISPER_BIN or put whisper-cli on PATH).'
        if not model_path().is_file():
            return f'Whisper model file not found at {model_path()} (set LOCAL_WHISPER_MODEL).'
        return ''

    def model_for(self, capability):
        return model_path().name

    def transcribe(self, audio, *, language=None, words=False, duration=None):
        """Cue dicts from any audio/video file. ``language`` is ignored (whisper-cli runs with ``-l auto``)."""
        with tempfile.TemporaryDirectory(prefix='local-whisper-') as tmp:
            with ledger.guarded(self, Capability.ASR, model=self.model_for(Capability.ASR),
                                units=float(duration or 0), unit_type='audio_seconds'):
                return local_transcription.transcribe_source(Path(audio), Path(tmp),
                                                             duration if duration is not None else float('inf'))
