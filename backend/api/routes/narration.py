"""Narration API (/api/v1/narration): voices, preview, synthesize, manual upload. Owner-scoped, session auth."""
import hashlib
import json
import logging
import math
import mimetypes
import os
import uuid
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from ninja import File, Form, Router, Schema
from ninja.errors import HttpError
from ninja.files import UploadedFile

from ..models import MediaAsset
from ..providers import Capability, ProviderRateLimited, registry
from ..services import assets as asset_svc
from ..services import narration as svc
from ..services.media import StudioError, disk_check
from .assets import serialize

log = logging.getLogger(__name__)
router = Router()
CLONE_HINTS = ('clone', 'cloning', 'reference', 'sample_audio', 'imitate', 'impersonat', 'similar_to', 'like_voice')


class NarrationIn(Schema):
    text: str
    language: str = 'en'
    voice_id: str
    speed: float = 1.0
    provider: Optional[str] = None


class PreviewIn(NarrationIn):
    text: Optional[str] = None


def reject_cloning(request):
    try:
        body = json.loads(request.body or b'{}')
    except ValueError:
        body = {}
    for key in (body if isinstance(body, dict) else {}):
        if any(h in key.lower() for h in CLONE_HINTS):
            raise StudioError('voice_cloning_unsupported',
                              'Voice cloning and imitating a real person are not supported. Choose a listed voice or upload your own recording.')


def asset_json(asset):
    return {**serialize(asset), 'metadata': asset.metadata}


def rate_limited(exc):
    until = exc.retry_after
    seconds = max(1, math.ceil((until - timezone.now()).total_seconds())) if until else \
        int(os.getenv('AI_RATE_LIMIT_COOLDOWN_SECONDS') or 60)
    return 429, {'error': exc.code, 'message': exc.message, 'retry_after_seconds': seconds}


@router.get('/v1/narration/voices')
def voices(request, language: str = ''):
    code = svc.normalize_language(language) if language.strip() else None
    out = []
    for name in svc.tts_provider_names():
        provider = registry.get(name)
        verdict = registry.evaluate(provider, Capability.TTS, language=code, estimate=provider.estimate('x' * 500))
        info = provider.info
        out.append({'name': name, 'label': info.label, 'available': verdict['usable'], 'reason': verdict['reason'],
                    'billing': str(info.billing), 'languages': list(info.languages) or ['multilingual'],
                    'commercial_use': provider.commercial_use(), 'notes': info.notes, 'required_env': list(info.required_env),
                    'voices': provider.voice_meta(code or 'en') if verdict['usable'] else []})
    return {'providers': out, 'manual_upload': True}


@router.post('/v1/narration/preview', response={200: dict, 429: dict})
def preview(request, data: PreviewIn):
    reject_cloning(request)
    try:
        asset = svc.preview_voice(request.auth, data.language, data.voice_id, data.speed, data.text, data.provider)
    except ProviderRateLimited as exc:
        return rate_limited(exc)
    return 200, asset_json(asset)


@router.post('/v1/narration/synthesize', response={200: dict, 429: dict})
def synthesize(request, data: NarrationIn):
    reject_cloning(request)
    try:
        asset = svc.synthesize_narration(request.auth, data.text, data.language, data.voice_id, data.speed,
                                         provider=data.provider)
    except ProviderRateLimited as exc:
        return rate_limited(exc)
    return 200, asset_json(asset)


@router.post('/v1/narration/upload', response={200: dict, 201: dict})
def upload(request, file: File[UploadedFile], i_recorded_this: Form[bool] = False, license_status: Form[str] = '',
           title: Form[str] = '', attribution_text: Form[str] = '', license_reference: Form[str] = '',
           rights_notes: Form[str] = ''):
    owner = request.auth
    if not license_status:
        license_status = 'owned' if i_recorded_this else 'unknown'
    elif license_status == 'owned' and not i_recorded_this:
        raise StudioError('declaration_required', 'Tick "I recorded this" to mark narration as owned.')
    asset_svc.check_rights(license_status, attribution_text)
    if max(len(title), len(attribution_text), len(license_reference), len(rights_notes)) > 5000 or len(title) > 300:
        raise HttpError(422, 'A text field is too long.')
    limit = asset_svc.max_asset_bytes()
    if file.size and file.size > limit:
        raise HttpError(413, 'Upload exceeds the asset file-size limit.')
    disk_check(settings.MEDIA_ROOT)
    if MediaAsset.objects.filter(owner=owner, deleted_at__isnull=True).count() >= asset_svc.max_assets_per_owner():
        raise StudioError('asset_limit', f'Library limit of {asset_svc.max_assets_per_owner()} assets reached.')
    folder = asset_svc.owner_dir(owner)
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder/f'.{uuid.uuid4().hex}.part'
    final = None
    try:
        digest, size = hashlib.sha256(), 0
        with temp.open('wb') as out:
            for chunk in file.chunks():
                size += len(chunk)
                if size > limit:
                    raise HttpError(413, 'Upload exceeds the asset file-size limit.')
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise StudioError('empty_file', 'The uploaded file is empty.')
        sha = digest.hexdigest()
        existing = MediaAsset.objects.filter(owner=owner, sha256=sha, deleted_at__isnull=True).first()
        if existing:
            if existing.kind != 'narration':
                raise StudioError('duplicate_other_kind', 'This file is already in your library as a different kind of asset.')
            return 200, {**asset_json(svc.attach_manual_narration(owner, existing)), 'deduplicated': True}
        info = asset_svc.probe_av(temp, 'audio')
        if info['duration'] < svc.MIN_SECONDS or info['duration'] > svc.max_seconds():
            raise StudioError('narration_duration', f'Narration must be between {svc.MIN_SECONDS:g} and {svc.max_seconds():g} seconds long.')
        final = folder/f'{uuid.uuid4()}{asset_svc.safe_ext(file.name, "narration")}'
        temp.replace(final)
        try:
            asset = MediaAsset.objects.create(
                owner=owner, kind='narration', title=(title.strip() or os.path.splitext(file.name or '')[0])[:300],
                file=asset_svc.relative(final), mime_type=mimetypes.guess_type(final.name)[0] or 'audio/mpeg',
                size_bytes=size, sha256=sha, duration=info['duration'], origin='manual_narration',
                license_status=license_status, license_reference=license_reference,
                attribution_text=attribution_text.strip(), rights_notes=rights_notes,
                metadata={'manual': True, 'i_recorded_this': bool(i_recorded_this), 'timing': 'none'})
        except IntegrityError:
            final.unlink(missing_ok=True)
            existing = MediaAsset.objects.filter(owner=owner, sha256=sha, deleted_at__isnull=True).first()
            if existing:
                return 200, {**asset_json(existing), 'deduplicated': True}
            raise
        final = None
        return 201, {**asset_json(svc.attach_manual_narration(owner, asset)), 'deduplicated': False}
    finally:
        temp.unlink(missing_ok=True)
        if final is not None:
            final.unlink(missing_ok=True)
