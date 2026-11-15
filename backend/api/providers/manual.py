"""Manual narration: the LOCAL pseudo-TTS. Always available; satisfied by an uploaded narration MediaAsset.

It never synthesizes speech. Callers that resolve TTS and get this provider must ask the user to upload
narration audio instead (the narration package wires that flow).
"""
from __future__ import annotations

from decimal import Decimal

from .base import Billing, Capability, Estimate, ProviderBase, ProviderInfo, ProviderUnavailable
from .registry import register


@register
class ManualNarration(ProviderBase):
    info = ProviderInfo(
        name='manual', label='Manual narration upload', capabilities=frozenset({Capability.TTS}),
        billing=Billing.LOCAL, commercial_use=True,
        notes='No speech is synthesized: upload your own narration audio.')

    def unavailable_reason(self, capability):
        return ''

    def voices(self, language):
        return []

    def synthesize(self, text, *, voice_id, speed, out):
        raise ProviderUnavailable('No text-to-speech provider is configured; upload narration audio instead.',
                                  'manual_narration_required')

    def estimate(self, text):
        return Estimate(units=len(text or ''), unit_type='characters', usd=Decimal(0))
