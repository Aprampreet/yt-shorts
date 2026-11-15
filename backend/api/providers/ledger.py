"""DB-backed provider usage ledger (``ProviderUsage`` rows).

The API and worker processes share the database, so cooldowns and daily spend agree across both.

    with ledger.guarded(provider, Capability.TEXT, model='m') as rec:   # policy gate + ledger row
        response = do_request()
        rec.http_status = response.status_code

Every hosted request attempt is one row. ``job.analysis['inference_calls']`` is ``job_calls(job)``.
"""
from __future__ import annotations

import email.utils
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Max, Sum
from django.utils import timezone

from .base import Billing, ProviderInvalid, ProviderPolicyBlocked, ProviderRateLimited, env_int

log = logging.getLogger(__name__)

COUNTED = ('ok', 'error', 'rate_limited', 'invalid_response')   # a request actually left the process
MAX_COOLDOWN = timedelta(hours=24)
_DURATION = re.compile(r'(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$')


def _header(headers, name):
    if not headers:
        return None
    try:
        value = headers.get(name)
        if value is None and isinstance(headers, dict):
            value = next((v for k, v in headers.items() if k.lower() == name), None)
        return value
    except Exception:  # Mock objects or odd header containers
        return None


def _seconds(value):
    """Parse '12', '12.5', '7.66s', '2m59.56s', '1h2m', '250ms' into seconds."""
    if value is None:
        return None
    text = str(value).strip().lower()
    try:
        return float(text)
    except ValueError:
        pass
    match = _DURATION.match(text)
    if not match or not any(match.groups()):
        return None
    h, m, s, ms = (float(g) if g else 0.0 for g in match.groups())
    return h*3600 + m*60 + s + ms/1000


def parse_retry_after(headers, now: datetime | None = None, body: dict | None = None) -> datetime | None:
    """Turn ``Retry-After`` (seconds or HTTP date), ``x-ratelimit-reset-*`` or a Google RetryInfo body into a time."""
    now = now or timezone.now()
    raw = _header(headers, 'retry-after')
    delay = _seconds(raw)
    if delay is None and raw:
        try:
            when = email.utils.parsedate_to_datetime(str(raw))
            delay = (when - now).total_seconds()
        except (TypeError, ValueError):
            delay = None
    if delay is None:
        resets = [_seconds(_header(headers, name)) for name in ('x-ratelimit-reset-requests', 'x-ratelimit-reset-tokens')]
        resets = [r for r in resets if r and r > 0]
        delay = min(resets) if resets else None
    if delay is None and isinstance(body, dict):
        for detail in (body.get('error') or {}).get('details') or []:
            if isinstance(detail, dict) and str(detail.get('@type', '')).endswith('RetryInfo'):
                delay = _seconds(detail.get('retryDelay'))
    if delay is None or delay <= 0:
        return None
    return now + min(timedelta(seconds=delay), MAX_COOLDOWN)


def default_cooldown(now=None):
    return (now or timezone.now()) + timedelta(seconds=env_int('AI_RATE_LIMIT_COOLDOWN_SECONDS', 60))


# --- queries --------------------------------------------------------------------------------------------

def day_start():
    """Start of the current UTC day; free tiers and budgets are counted per UTC day."""
    return timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)


def cooldown_until(name: str) -> datetime | None:
    from ..models import ProviderUsage
    return ProviderUsage.objects.filter(provider=name, retry_after_until__gt=timezone.now()).aggregate(
        until=Max('retry_after_until'))['until']


def spent_today() -> Decimal:
    from ..models import ProviderUsage
    total = ProviderUsage.objects.filter(created_at__gte=day_start(), status__in=COUNTED).aggregate(
        total=Sum('cost_usd_estimate'))['total']
    return Decimal(total or 0)


def calls_today(*, provider: str | None = None, billing=None) -> int:
    from ..models import ProviderUsage
    rows = ProviderUsage.objects.filter(created_at__gte=day_start(), status__in=COUNTED)
    if provider:
        rows = rows.filter(provider=provider)
    if billing:
        rows = rows.filter(billing__in=[str(b) for b in billing])
    return rows.count()


def job_calls(job, *, billing=None) -> int:
    """Hosted inference requests made for a job (local providers are not counted)."""
    if job is None or getattr(job, 'pk', None) is None:
        return 0
    from ..models import ProviderUsage
    rows = ProviderUsage.objects.filter(job_id=job.pk, status__in=COUNTED)
    rows = rows.filter(billing__in=[str(b) for b in billing]) if billing else rows.exclude(billing=str(Billing.LOCAL))
    return rows.count()


# --- recording ------------------------------------------------------------------------------------------

@dataclass
class CallRecord:
    model: str = ''
    units: float = 1
    unit_type: str = 'calls'
    cost_usd: Decimal | None = None
    http_status: int | None = None
    status: str | None = None          # caller may force e.g. 'error' when a wrapped call returned nothing
    retry_after_until: datetime | None = None


def write(info, capability, *, job=None, status='ok', model='', units=1, unit_type='calls', cost_usd=None,
          http_status=None, latency_ms=None, retry_after_until=None):
    from ..models import ProviderUsage
    job_id = getattr(job, 'pk', None)
    return ProviderUsage.objects.create(
        owner=(getattr(job, 'owner', '') or '')[:64], job_id=job_id, provider=info.name, capability=str(capability),
        model=(model or '')[:120], billing=str(info.billing), units=units, unit_type=unit_type,
        cost_usd_estimate=cost_usd, status=status, http_status=http_status, latency_ms=latency_ms,
        retry_after_until=retry_after_until)


@contextmanager
def call(info, capability, *, job=None, model='', units=1, unit_type='calls', cost_usd=None):
    """Record one request attempt. Exceptions propagate after the row is written."""
    rec = CallRecord(model=model, units=units, unit_type=unit_type, cost_usd=cost_usd)
    started = time.monotonic()
    status = 'ok'
    try:
        yield rec
    except ProviderRateLimited as exc:
        status = 'rate_limited'
        rec.http_status = rec.http_status or exc.http_status or 429
        rec.retry_after_until = exc.retry_after or default_cooldown()
        raise
    except ProviderPolicyBlocked:
        status = 'blocked_by_policy'
        raise
    except ProviderInvalid as exc:
        status = 'invalid_response'
        rec.http_status = rec.http_status or exc.http_status
        raise
    except BaseException as exc:
        status = 'error'
        response = getattr(exc, 'response', None)
        rec.http_status = rec.http_status or getattr(exc, 'http_status', None) or getattr(response, 'status_code', None)
        if rec.http_status == 429:
            status = 'rate_limited'
            rec.retry_after_until = parse_retry_after(getattr(response, 'headers', None)) or default_cooldown()
        raise
    finally:
        if rec.status:
            status = rec.status
        if status == 'rate_limited' and not rec.retry_after_until:
            rec.retry_after_until = default_cooldown()
        try:
            write(info, capability, job=job, status=status, model=rec.model, units=rec.units, unit_type=rec.unit_type,
                  cost_usd=rec.cost_usd if status in COUNTED else None, http_status=rec.http_status,
                  latency_ms=int((time.monotonic()-started)*1000), retry_after_until=rec.retry_after_until)
        except Exception:
            # Never hide the provider outcome behind an accounting failure; but make it loud.
            log.exception('Could not write provider usage row for %s', info.name)


@contextmanager
def guarded(provider, capability, *, model='', estimate=None, units=1, unit_type='calls'):
    """Policy gate, then one ledger row. Providers built with ``record=False`` (legacy direct construction,
    e.g. ``HuggingFaceProvider()`` in tests) only get the env-level policy check and write no rows."""
    from . import policy
    info, job = provider.info, getattr(provider, 'job', None)
    record = getattr(provider, 'record', True)
    decision = policy.check(info, estimate, job, use_db=record)
    if not decision.allowed:
        if record:
            write(info, capability, job=job, status='blocked_by_policy', model=model, units=0)
        decision.raise_for_block()
    if not record:
        yield CallRecord(model=model, units=units, unit_type=unit_type)
        return
    with call(info, capability, job=job, model=model, units=units, unit_type=unit_type,
              cost_usd=getattr(estimate, 'usd', None)) as rec:
        yield rec
