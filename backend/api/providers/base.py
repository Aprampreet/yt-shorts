"""Provider contracts: capabilities, billing classes, availability, Protocols and errors.

Nothing here performs network I/O. Adapters live in sibling modules and register with ``registry``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from ..services.media import StudioError


class Capability(StrEnum):
    ASR = 'asr'
    TRANSLATION = 'translation'
    TEXT = 'text'
    VISION = 'vision'
    TTS = 'tts'


class Billing(StrEnum):
    LOCAL = 'local_free'          # runs on this machine; never billed
    FREE_TIER = 'free_tier'       # hosted free tier; account-dependent limits
    METERED = 'metered_unknown'   # may bill, cost per call unknown up front (e.g. HF "auto" routing)
    PAID = 'paid'                 # billed with a known per-unit estimate


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    label: str
    capabilities: frozenset
    billing: Billing
    required_env: tuple = ()
    languages: tuple = ()            # empty = not restricted by this app / multilingual
    commercial_use: bool | None = None
    rate_limit_rpm: int | None = None
    notes: str = ''

    def to_dict(self):
        return {'name': self.name, 'label': self.label, 'capabilities': sorted(str(c) for c in self.capabilities),
                'billing': str(self.billing), 'required_env': list(self.required_env),
                'languages': list(self.languages), 'commercial_use': self.commercial_use,
                'rate_limit_rpm': self.rate_limit_rpm, 'notes': self.notes}


@dataclass
class Availability:
    available: bool
    reason: str
    billing: Billing
    cooldown_until: datetime | None = None
    capabilities: dict = field(default_factory=dict)   # per-capability {cap: bool} when it differs by env

    def to_dict(self):
        return {'available': self.available, 'reason': self.reason, 'billing': str(self.billing),
                'cooldown_until': self.cooldown_until.isoformat() if self.cooldown_until else None,
                'capabilities': {str(k): v for k, v in self.capabilities.items()}}


@dataclass(frozen=True)
class Estimate:
    units: float = 1
    unit_type: str = 'calls'
    usd: Decimal | None = None


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Voice:
    id: str
    name: str
    language: str


@dataclass(frozen=True)
class Synthesis:
    path: Path
    duration: float
    words: list | None
    characters: int


# --- Protocols -----------------------------------------------------------------------------------------

@runtime_checkable
class BaseProvider(Protocol):
    info: ProviderInfo

    def availability(self) -> Availability: ...


@runtime_checkable
class ASRProvider(BaseProvider, Protocol):
    def transcribe(self, audio: Path, *, language: str | None = None, words: bool = False,
                   duration: float | None = None) -> list[dict]: ...


@runtime_checkable
class TranslationProvider(BaseProvider, Protocol):
    def translate(self, cues: list[dict]) -> list: ...


@runtime_checkable
class TextProvider(BaseProvider, Protocol):
    def complete_json(self, *, task: str, system: str, user: str, schema: type[BaseModel],
                      max_tokens: int) -> BaseModel: ...


@runtime_checkable
class VisionProvider(BaseProvider, Protocol):
    def describe(self, frames: list[Path], schema: type[BaseModel], *, prompt: str = '',
                 max_tokens: int = 800) -> BaseModel: ...


@runtime_checkable
class TTSProvider(BaseProvider, Protocol):
    """Interface only in WP3; concrete adapters arrive with the narration package."""
    def voices(self, language: str) -> list[Voice]: ...
    def synthesize(self, text: str, *, voice_id: str, speed: float, out: Path) -> Synthesis: ...
    def estimate(self, text: str) -> Estimate: ...


class ProviderBase:
    """Shared plumbing for adapters. ``job`` attributes ledger rows; ``record=False`` disables the DB-backed
    ledger and limits (used only by legacy direct construction such as ``HuggingFaceProvider()``)."""
    info: ProviderInfo

    def __init__(self, *, job=None, record: bool = True):
        self.job, self.record = job, record

    def unavailable_reason(self, capability: Capability | None) -> str:
        """Env/binary/model-file checks only (no network). Empty string means configured."""
        return ''

    def availability(self, capability: Capability | None = None) -> Availability:
        caps = [Capability(capability)] if capability else sorted(self.info.capabilities)
        reasons = {cap: self.unavailable_reason(cap) for cap in caps}
        ready = [cap for cap in caps if not reasons[cap]]
        missing = list(dict.fromkeys(r for r in reasons.values() if r))
        if not ready:
            reason = '; '.join(missing)
        elif missing:
            reason = 'Partly configured: ' + '; '.join(f'{cap}: {reasons[cap]}' for cap in caps if reasons[cap])
        else:
            reason = 'Configured.'
        return Availability(bool(ready), reason, self.info.billing,
                            capabilities={cap: not reasons[cap] for cap in caps})

    def adapter(self, capability: Capability):
        """Object implementing the Protocol for ``capability`` (usually ``self``)."""
        return self

    def model_for(self, capability: Capability) -> str:
        return ''


# --- Errors (codes are the existing StudioError codes) ------------------------------------------------

class ProviderError(StudioError):
    default_code = 'ai_unavailable'

    def __init__(self, message, code=None, *, http_status=None):
        super().__init__(code or self.default_code, message)
        self.http_status = http_status


class ProviderUnavailable(ProviderError):
    default_code = 'ai_unavailable'


class ProviderPolicyBlocked(ProviderError):
    default_code = 'paid_inference_disabled'


class ProviderRateLimited(ProviderError):
    default_code = 'ai_rate_limited'

    def __init__(self, message, retry_after: datetime | None = None, code=None, *, http_status=429):
        super().__init__(message, code, http_status=http_status)
        self.retry_after = retry_after


class ProviderInvalid(ProviderError):
    default_code = 'ai_invalid_response'


# --- Env helpers (read at call time: tests patch os.environ) ------------------------------------------

def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() == 'true'   # same parsing as settings.py and the legacy checks


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, '') or default)
    except ValueError:
        return default


def paid_allowed() -> bool:
    return env_flag('AI_ALLOW_PAID_PROVIDERS') or env_flag('AI_ALLOW_BILLABLE')


def schema_of(schema: Any) -> dict:
    return schema.model_json_schema() if hasattr(schema, 'model_json_schema') else dict(schema)


# Keywords kept in a strict (Groq / OpenAI-style) schema. Groq's structured-outputs guide (checked 2026-11-15)
# documents types, object/array/enum, anyOf, $defs/$ref and nullable unions, requires every property in
# ``required`` and ``additionalProperties: false`` on every object, and does not document length/size/format
# keywords, so those are dropped. The response is still validated app-side against the original Pydantic model.
STRICT_KEYWORDS = frozenset({'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'anyOf',
                             '$defs', '$ref', 'description'})


def strict_schema(schema: Any) -> dict:
    """Pure: convert a Pydantic model (or JSON schema dict) into the strict structured-output subset.

    * every object lists all its properties in ``required`` and sets ``additionalProperties: false``;
    * unsupported keywords (minLength, maxLength, minItems, maxItems, pattern, format, default, title, examples,
      minimum, maximum, ...) are dropped; ``const`` becomes a one-value ``enum``; ``oneOf`` becomes ``anyOf``;
    * fields that had a default become required *without* being made nullable, so the model must emit a value of
      the declared type (e.g. ``''`` or ``[]``); fields whose type already allows null keep their ``anyOf`` null
      branch. This keeps the result valid for the unchanged Pydantic model (a null for a ``str`` would not be).
    * ``$ref`` siblings are dropped (a ``$ref`` node stays a bare reference); ``$defs`` are kept at the root.
    """
    return _strict_node(schema_of(schema), root=True)


def _strict_node(node, root=False):
    if isinstance(node, list):
        return [_strict_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    if '$ref' in node:
        return {'$ref': node['$ref']}
    out = {}
    for key, value in node.items():
        if key == 'const':
            out['enum'] = [value]
        elif key == 'oneOf':
            out['anyOf'] = _strict_node(value)
        elif key == 'allOf' and isinstance(value, list) and len(value) == 1:
            out.update(_strict_node(value[0]))           # Pydantic wraps single refs this way
        elif key in ('properties', '$defs') and isinstance(value, dict):
            out[key] = {name: _strict_node(sub) for name, sub in value.items()}
        elif key in ('items', 'anyOf'):
            out[key] = _strict_node(value)
        elif key in STRICT_KEYWORDS and key not in ('required', 'additionalProperties'):
            out[key] = value
    if not root:
        out.pop('$defs', None)   # Pydantic only emits $defs at the root
    types = out.get('type')
    is_object = types == 'object' or (isinstance(types, list) and 'object' in types) or 'properties' in out
    if is_object:
        out.setdefault('type', 'object')
        out.setdefault('properties', {})
        out['required'] = list(out['properties'])
        out['additionalProperties'] = False
    if types == 'array' and 'items' not in out:
        out['items'] = {'type': 'string'}
    return out
