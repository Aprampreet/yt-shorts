"""Narration: synthesize with a free hosted provider, cache voice previews, or attach an uploaded recording.

No audio is ever fabricated: with nothing configured every call raises a clear error and the manual upload path
(``attach_manual_narration`` / POST /v1/narration/upload) remains the fallback. Paid providers are used only when
named explicitly; automatic selection considers free-tier providers only.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..models import MediaAsset
from ..providers import Billing, Capability, ProviderRateLimited, ProviderUnavailable, ledger, registry
from ..story.alignment import align_words
from . import assets as asset_svc
from .media import StudioError, disk_check

MAX_CHARS = 2500
MAX_PREVIEW_CHARS = 240
MIN_SPEED, MAX_SPEED = 0.8, 1.25
PREVIEW_RETENTION = timedelta(days=1)
MIN_SECONDS = 1.0
SAMPLES = {'en': 'Hello, this is a short preview of this voice.',
           'hi': 'नमस्ते, यह इस आवाज़ का एक छोटा नमूना है।'}


def max_seconds():
    try:
        return float(os.getenv('NARRATION_MAX_SECONDS') or 600)
    except ValueError:
        return 600.0


def normalize_language(language):
    code = (language or 'en').strip().lower().replace('_', '-')
    if code in ('hinglish', 'hi-latn'):
        code = 'hi'
    code = code.split('-')[0]
    if not re.fullmatch(r'[a-z]{2,3}', code):
        raise StudioError('invalid_language', 'language must be a language code such as "en" or "hi".')
    return code


def tts_provider_names():
    return [n for n, c in registry.PROVIDERS.items() if Capability.TTS in c.info.capabilities and n != 'manual']


def _check_language(adapter, language):
    supported = adapter.info.languages
    if supported and language not in supported:
        raise StudioError('language_unsupported',
                          f'{adapter.info.label} supports only {", ".join(supported)}; it cannot narrate "{language}". '
                          'Choose a multilingual provider or upload your own narration.')


def _adapter(provider, language, job):
    if provider:
        if provider == 'manual':
            raise StudioError('manual_narration_required', 'Manual narration is an upload: use POST /v1/narration/upload.')
        if provider not in tts_provider_names():
            raise StudioError('unknown_provider', f'"{provider}" is not a text-to-speech provider.')
        adapter = registry.get(provider, job=job)
        _check_language(adapter, language)
        return adapter
    skip = ['manual'] + [n for n in tts_provider_names() if registry.PROVIDERS[n].info.billing != Billing.FREE_TIER]
    try:
        return registry.resolve(Capability.TTS, language=language, job=job, exclude=tuple(skip))
    except ProviderUnavailable as exc:
        cooling = []
        for name in tts_provider_names():
            candidate = registry.get(name, job=job)
            languages = candidate.info.languages
            if name in skip or not candidate.availability(Capability.TTS).available or (languages and language not in languages):
                continue
            until = ledger.cooldown_until(name)
            if until:
                cooling.append(until)
        if cooling:
            until = min(cooling)
            raise ProviderRateLimited(f'Text-to-speech is rate limited until {until:%H:%M} UTC; try again later or upload narration.', until) from None
        error = ProviderUnavailable(f'{exc.message} You can upload your own narration audio instead.', 'tts_unavailable')
        error.reasons = getattr(exc, 'reasons', [])
        raise error from None


def license_for(adapter):
    """(license_status, rights_notes) from the provider's recorded commercial-use position."""
    commercial = adapter.commercial_use()
    label = adapter.info.label
    if commercial is True:
        return 'commercial_use_permitted', f'Synthesised by {label}; its terms were checked and do not restrict commercial use of the output.'
    if commercial is False:
        return 'restricted', (f'Synthesised by {label} on a plan without a commercial licence '
                              '(ELEVENLABS_PLAN_COMMERCIAL is not true). Do not use in monetised videos unless your plan allows it.')
    return 'unknown', f'Commercial-use terms for {label} output were not verified; review them before monetising.'


def _validate(text, speed, limit):
    text = (text or '').strip()
    if not text:
        raise StudioError('empty_text', 'There is no text to narrate.')
    if len(text) > limit:
        raise StudioError('text_too_long', f'Text is limited to {limit} characters here ({len(text)} given).')
    if not MIN_SPEED <= float(speed) <= MAX_SPEED:
        raise StudioError('invalid_speed', f'Speed must be between {MIN_SPEED} and {MAX_SPEED}.')
    return text


def _synthesize(owner, text, language, voice, speed, job, provider, *, extra=None, retention=None, title='', align=True):
    language = normalize_language(language)
    adapter = _adapter(provider, language, job)
    disk_check(settings.MEDIA_ROOT)
    folder = asset_svc.owner_dir(owner)/'tts'
    out = folder/f'{uuid.uuid4()}.wav'
    asset = None
    try:
        try:
            result = adapter.synthesize(text, voice_id=voice, speed=float(speed), out=out)
        except ProviderRateLimited as exc:
            exc.retry_after = exc.retry_after or ledger.cooldown_until(adapter.info.name)
            raise
        timing = align_words(text, out if align else None, result.duration, result.words)
        status, notes = license_for(adapter)
        metadata = {'voice': voice, 'speed': float(speed), 'language': language, 'characters': result.characters,
                    'provider_label': adapter.info.label, 'provider_notes': adapter.info.notes,
                    'commercial_use': adapter.commercial_use(), 'words': timing['words'], 'timing': timing['timing'],
                    **(extra or {})}
        asset = asset_svc.register_generated_asset(owner, out, 'narration', 'generated_tts', status,
                                                   provider=adapter.info.name, metadata=metadata,
                                                   title=title or text[:60])
        changed = []
        if not asset.rights_notes:
            asset.rights_notes, changed = notes, ['rights_notes']
        if retention is not None and asset.metadata.get('preview'):
            asset.retention_until = timezone.now() + retention
            changed.append('retention_until')
        if changed:
            asset.save(update_fields=changed)
        return asset
    finally:
        if out.exists() and not (asset is not None and asset_svc.relative(out) == asset.file):
            out.unlink(missing_ok=True)


def synthesize_narration(owner, text, language, voice, speed=1.0, job=None, provider=None):
    text = _validate(text, speed, MAX_CHARS)
    return _synthesize(owner, text, language, voice, speed, job, provider)


def preview_voice(owner, language, voice, speed=1.0, text=None, provider=None, job=None):
    language_code = normalize_language(language)
    text = _validate(text or SAMPLES.get(language_code, SAMPLES['en']), speed, MAX_PREVIEW_CHARS)
    key = hashlib.sha256(f'{provider or "auto"}|{voice}|{language_code}|{float(speed):.3f}|{text}'.encode()).hexdigest()
    cached = MediaAsset.objects.filter(owner=owner, deleted_at__isnull=True, metadata__preview_key=key,
                                       retention_until__gt=timezone.now()).first()
    if cached:
        try:
            asset_svc.asset_path(cached)
            return cached
        except StudioError:
            pass
    return _synthesize(owner, text, language, voice, speed, job, provider, retention=PREVIEW_RETENTION,
                       title=f'Voice preview: {voice}', align=False,
                       extra={'preview': True, 'preview_key': key})


def attach_manual_narration(owner, asset):
    """Validate an uploaded narration asset (owner's, kind narration, audio present, duration bounds)."""
    if asset.owner != owner or asset.deleted_at is not None:
        raise StudioError('asset_unavailable', 'Narration asset not found.')
    if asset.kind != 'narration':
        raise StudioError('invalid_kind', 'Only assets of kind "narration" can be used as narration.')
    info = asset_svc.probe_av(asset_svc.asset_path(asset), 'audio')
    if info['duration'] < MIN_SECONDS:
        raise StudioError('narration_too_short', f'Narration must be at least {MIN_SECONDS:g} second long.')
    if info['duration'] > max_seconds():
        raise StudioError('narration_too_long', f'Narration must be at most {max_seconds():g} seconds long.')
    if asset.duration != info['duration']:
        asset.duration = info['duration']
        asset.save(update_fields=['duration'])
    return asset
