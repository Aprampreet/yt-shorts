"""Compatibility shim. The provider layer lives in ``api.providers``; these names stay importable from here."""
from typing import Protocol

from ..providers import (ASRProvider, BaseProvider, TextProvider, TranslationProvider, TTSProvider,  # noqa: F401
                         VisionProvider)
from ..providers.huggingface import (EditorialAnalysis, GroundedCommentary, HuggingFaceProvider,  # noqa: F401
                                     TranslationPair, TranslationResult)


class TranscriptionProvider(Protocol):
    def transcribe(self, audio): ...


class VisionAnalysisProvider(Protocol):
    def analyze_frame(self, image): ...


class TextAnalysisProvider(Protocol):
    def analyze(self, transcript): ...


class SpeechSynthesisProvider(Protocol):
    def synthesize(self, text): ...
