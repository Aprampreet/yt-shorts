"""Provider registry: ordering, resolution and status.

    from api.providers import Capability, registry
    text = registry.resolve(Capability.TEXT, job=job)        # first available, policy-allowed provider
    result = text.complete_json(task='...', system='...', user='...', schema=MyModel, max_tokens=600)

``resolve`` never fakes a provider: when nothing qualifies it raises ``ProviderUnavailable`` listing each
provider's reason. Order per capability comes from ``AI_PROVIDER_ORDER_<CAP>`` (comma separated names; an
explicit list also disables providers left out of it), else the local-first defaults below.
"""
from __future__ import annotations

import os

from . import ledger, policy
from .base import Capability, ProviderUnavailable

PROVIDERS: dict = {}       # name -> provider class

DEFAULT_ORDER = {
    Capability.ASR: ('local_whisper', 'groq', 'huggingface'),
    Capability.TEXT: ('groq', 'gemini', 'huggingface'),
    Capability.VISION: ('groq', 'gemini'),
    Capability.TRANSLATION: ('huggingface',),
    Capability.TTS: ('manual',),
}


def register(cls):
    PROVIDERS[cls.info.name] = cls
    return cls


def get(name: str, *, job=None, record: bool = True):
    try:
        cls = PROVIDERS[name]
    except KeyError:
        raise ProviderUnavailable(f'Unknown provider "{name}".') from None
    return cls(job=job, record=record)


def order(capability) -> list[str]:
    capability = Capability(capability)
    supports = [name for name, cls in PROVIDERS.items() if capability in cls.info.capabilities]
    configured = os.getenv(f'AI_PROVIDER_ORDER_{capability.name}', '').strip()
    if configured:
        names = [n.strip() for n in configured.split(',') if n.strip()]
        return [n for n in dict.fromkeys(names) if n in supports]
    defaults = [n for n in DEFAULT_ORDER.get(capability, ()) if n in supports]
    return defaults + [n for n in supports if n not in defaults]   # extra @register providers go last


def evaluate(provider, capability, *, language=None, job=None, estimate=None, use_db=True) -> dict:
    """Availability (env) + policy for one provider/capability, without making any request."""
    capability = Capability(capability)
    availability = provider.availability(capability)
    info = provider.info
    result = {'configured': availability.available, 'allowed': False, 'usable': False,
              'reason': availability.reason, 'code': '' if availability.available else 'ai_unavailable'}
    if not availability.available:
        return result
    if language and info.languages and language.split('-')[0] not in info.languages:
        result.update(reason=f'{info.label} does not support language "{language}" for {capability}.', code='ai_unavailable')
        return result
    decision = policy.check(info, estimate, job, use_db=use_db)
    result.update(allowed=decision.allowed, usable=decision.allowed,
                  reason='Ready.' if decision.allowed else decision.reason, code=decision.code)
    return result


def resolve(capability, *, language=None, job=None, estimate=None, exclude=()):
    """Return the adapter (bound to ``job`` for the ledger) of the first usable provider for ``capability``."""
    capability = Capability(capability)
    reasons = []
    for name in order(capability):
        if name in exclude:
            continue
        provider = get(name, job=job)
        verdict = evaluate(provider, capability, language=language, job=job, estimate=estimate)
        if verdict['usable']:
            return provider.adapter(capability)
        reasons.append(f'{provider.info.label}: {verdict["reason"]}')
    detail = '; '.join(reasons) if reasons else 'no provider is registered for it'
    error = ProviderUnavailable(f'No {capability} provider is available ({detail}).')
    error.reasons = reasons
    raise error


def usable(name: str, capability, *, job=None) -> bool:
    return bool(name in PROVIDERS and evaluate(get(name, job=job), capability, job=job)['usable'])


def status(*, probe: bool = False) -> dict:
    """Snapshot for GET /v1/providers and /v1/capabilities. Network only when ``probe`` is true."""
    providers = []
    for name, cls in PROVIDERS.items():
        provider = cls(job=None, record=True)
        caps = {}
        for capability in sorted(cls.info.capabilities):
            caps[str(capability)] = evaluate(provider, capability)
        usable_caps = [c for c, v in caps.items() if v['usable']]
        first_reason = next((v['reason'] for v in caps.values() if not v['usable']), '')
        entry = dict(cls.info.to_dict(), available=bool(usable_caps),
                     reason='Ready.' if usable_caps and len(usable_caps) == len(caps) else
                            (f'Ready for {", ".join(usable_caps)}; ' + first_reason if usable_caps else first_reason),
                     capability_status=caps, cooldown_until=None, calls_today=0)
        if cls.info.billing != 'local_free':
            until = ledger.cooldown_until(name)
            entry['cooldown_until'] = until.isoformat() if until else None
            entry['calls_today'] = ledger.calls_today(provider=name)
        if probe and hasattr(provider, 'probe'):
            entry['probe'] = provider.probe()
        providers.append(entry)
    return {'providers': providers,
            'order': {str(cap): order(cap) for cap in Capability},
            'policy': {'allow_free_tier': policy.env_flag('AI_ALLOW_FREE_TIER', True),
                       'allow_paid_providers': policy.paid_allowed(),
                       'daily_budget_usd': str(policy.daily_budget()),
                       'spent_today_usd': str(ledger.spent_today())}}
