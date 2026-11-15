"""Spending policy. Every hosted call passes ``check`` (via ``ledger.guarded``) before it leaves the process.

    local_free       always allowed
    free_tier        AI_ALLOW_FREE_TIER (default true), not cooling down, under AI_FREE_TIER_DAILY_CALLS (0 = no app cap)
    metered_unknown  AI_ALLOW_PAID_PROVIDERS=true (or legacy AI_ALLOW_BILLABLE=true), not cooling down,
                     under AI_MAX_CALLS_PER_JOB (metered+paid calls of the job) and AI_DAILY_PAID_CALLS (0 = no cap)
    paid             as metered, plus a per-call USD estimate and spent_today + estimate <= AI_DAILY_BUDGET_USD
                     (default 0, so paid calls are blocked with an explicit message)

All variables are read at call time; settings.py values are not cached here because tests patch os.environ.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from . import ledger
from .base import Billing, ProviderPolicyBlocked, ProviderRateLimited, env_flag, env_int, paid_allowed

PAID_DISABLED_MESSAGE = ('Hosted inference is disabled because this provider may charge. Enable only after explicitly '
                         'approving billing (AI_ALLOW_PAID_PROVIDERS=true).')


@dataclass(frozen=True)
class Decision:
    allowed: bool
    code: str = ''
    reason: str = ''
    retry_after: datetime | None = None

    def raise_for_block(self):
        if self.allowed:
            return
        if self.code == 'ai_rate_limited':
            raise ProviderRateLimited(self.reason, self.retry_after)
        raise ProviderPolicyBlocked(self.reason, self.code)


ALLOW = Decision(True)


def daily_budget() -> Decimal:
    try:
        value = Decimal(os.getenv('AI_DAILY_BUDGET_USD', '0') or '0')
    except InvalidOperation:
        return Decimal(0)
    return value if value.is_finite() and value > 0 else Decimal(0)


def check(info, estimate=None, job=None, *, use_db: bool = True) -> Decision:
    """Decide whether ``info``'s provider may make one call now. ``use_db=False`` skips the ledger-backed
    limits (cooldown, daily/job counts, spend) and applies only the env switches."""
    billing = info.billing
    if billing == Billing.LOCAL:
        return ALLOW
    if use_db:
        until = ledger.cooldown_until(info.name)
        if until:
            return Decision(False, 'ai_rate_limited',
                            f'{info.label} is rate limited until {until:%Y-%m-%d %H:%M} UTC; try again later.', until)
    if billing == Billing.FREE_TIER:
        if not env_flag('AI_ALLOW_FREE_TIER', True):
            return Decision(False, 'free_tier_disabled',
                            f'{info.label} is a free-tier hosted provider and AI_ALLOW_FREE_TIER=false; only local providers run.')
        cap = env_int('AI_FREE_TIER_DAILY_CALLS', 0)
        if use_db and cap and ledger.calls_today(provider=info.name) >= cap:
            return Decision(False, 'ai_budget', f'{info.label} reached AI_FREE_TIER_DAILY_CALLS={cap} for today (UTC).')
        return ALLOW
    # metered_unknown / paid
    if not paid_allowed():
        return Decision(False, 'paid_inference_disabled', PAID_DISABLED_MESSAGE)
    if use_db:
        per_job = env_int('AI_MAX_CALLS_PER_JOB', 30)
        if job is not None and ledger.job_calls(job, billing=(Billing.METERED, Billing.PAID)) >= per_job:
            return Decision(False, 'ai_budget', 'Per-job inference call limit reached; lightweight fallback is active.')
        daily = env_int('AI_DAILY_PAID_CALLS', 0)
        if daily and ledger.calls_today(billing=(Billing.METERED, Billing.PAID)) >= daily:
            return Decision(False, 'ai_budget', f'AI_DAILY_PAID_CALLS={daily} paid/metered calls were already made today (UTC).')
    if billing == Billing.PAID:
        budget = daily_budget()
        if budget <= 0:
            return Decision(False, 'ai_budget', f'{info.label} bills per use and AI_DAILY_BUDGET_USD is 0, so it is blocked. '
                                                'Set a positive daily budget in US dollars to allow it.')
        usd = getattr(estimate, 'usd', None)
        if usd is None:
            return Decision(False, 'ai_budget', f'{info.label} has no cost estimate for this call; blocked to protect the daily budget.')
        spent = ledger.spent_today() if use_db else Decimal(0)
        if spent + Decimal(usd) > budget:
            return Decision(False, 'ai_budget', f'Daily AI budget reached: ${spent} spent + ${usd} estimated exceeds '
                                                f'AI_DAILY_BUDGET_USD=${budget}.')
    return ALLOW


def enforce(info, estimate=None, job=None, *, use_db: bool = True):
    check(info, estimate, job, use_db=use_db).raise_for_block()
