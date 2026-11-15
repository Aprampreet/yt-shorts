"""Provider layer: capabilities, policy gate, usage ledger and adapters.

    from api.providers import Capability, registry
    asr = registry.resolve(Capability.ASR, job=job)
    cues = asr.transcribe(audio_path, words=True, duration=seconds)

Importing this package registers every built-in adapter.
"""
from .base import (ASRProvider, Availability, BaseProvider, Billing, Capability, Estimate, ProviderBase,  # noqa: F401
                   ProviderError, ProviderInfo, ProviderInvalid, ProviderPolicyBlocked, ProviderRateLimited,
                   ProviderUnavailable, Synthesis, TextProvider, TranslationProvider, TTSProvider, VisionProvider,
                   Voice, Word)
from . import registry, policy, ledger  # noqa: F401
from . import local_whisper, groq_adapter, gemini, huggingface, manual  # noqa: F401  (registration)
from . import tts_gemini, tts_groq, tts_elevenlabs  # noqa: F401  (registration)
