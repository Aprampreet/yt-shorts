"""Media Library: owner-scoped assets with explicit rights metadata. Mounted at /api/v1/assets."""
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, TextField
from django.db.models.functions import Cast
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import Router, Schema, File, Form
from ninja.errors import HttpError
from ninja.files import UploadedFile
from ..models import MediaAsset
from ..services import assets as svc
from ..services.media import StudioError, disk_check
from .files import private_file

log = logging.getLogger(__name__)
router = Router()
KINDS = [k for k, _ in MediaAsset.KIND_CHOICES]


def serialize(asset, usage=None):
    if usage is None:
        usage = dict(scenes=asset.scenes.count(),
                     projects=asset.scenes.values('script__project').distinct().count(),
                     narrations=asset.narrated_scripts.count())
    base = f'/api/v1/assets/{asset.pk}'
    return {
        'id': str(asset.pk), 'kind': asset.kind, 'title': asset.title, 'origin': asset.origin,
        'mime_type': asset.mime_type, 'size_bytes': asset.size_bytes, 'duration': asset.duration,
        'width': asset.width, 'height': asset.height, 'sha256': asset.sha256,
        'license_status': asset.license_status, 'license_label': svc.LICENSE_LABELS.get(asset.license_status, asset.license_status),
        'review_required': svc.review_required(asset), 'attribution_text': asset.attribution_text,
        'license_reference': asset.license_reference, 'rights_notes': asset.rights_notes,
        'source_url': asset.source_url, 'provider': asset.provider, 'tags': asset.tags,
        'pinned': asset.pinned, 'retention_until': asset.retention_until.isoformat() if asset.retention_until else None,
        'created_at': asset.created_at.isoformat(), 'deleted_at': asset.deleted_at.isoformat() if asset.deleted_at else None,
        'usage': usage, 'file_url': f'{base}/file',
        'thumbnail_url': f'{base}/thumbnail' if svc.has_thumbnail(asset) else None,
    }


def with_usage(qs):
    return qs.annotate(n_scenes=Count('scenes', distinct=True),
                       n_projects=Count('scenes__script__project', distinct=True),
                       n_narrations=Count('narrated_scripts', distinct=True))


def serialize_annotated(asset):
    return serialize(asset, dict(scenes=asset.n_scenes, projects=asset.n_projects, narrations=asset.n_narrations))


def owned_asset(request, asset_id, include_deleted=False):
    qs = MediaAsset.objects.filter(owner=request.auth)
    if not include_deleted:
        qs = qs.filter(deleted_at__isnull=True)
    return get_object_or_404(qs, pk=asset_id)


def clean_source_url(value):
    value = (value or '').strip()
    if not value:
        return ''
    if len(value) > 200 or not value.lower().startswith(('http://', 'https://')):
        raise StudioError('invalid_source_url', 'Source URL must be an http(s) URL of at most 200 characters.')
    try:
        URLValidator(schemes=['http', 'https'])(value)
    except ValidationError:
        raise StudioError('invalid_source_url', 'Source URL is not valid.')
    return value


@router.post('/v1/assets', response={200: dict, 201: dict})
def upload_asset(request, file: File[UploadedFile], kind: Form[str], license_status: Form[str],
                 title: Form[str] = '', attribution_text: Form[str] = '', license_reference: Form[str] = '',
                 rights_notes: Form[str] = '', source_url: Form[str] = '', tags: Form[str] = ''):
    owner = request.auth
    if kind not in svc.UPLOAD_KINDS:
        raise StudioError('invalid_kind', 'kind must be one of: ' + ', '.join(sorted(svc.UPLOAD_KINDS)) + '.')
    svc.check_rights(license_status, attribution_text)
    tag_list = svc.normalize_tags(tags)
    source_url = clean_source_url(source_url)
    if max(len(title), len(attribution_text), len(license_reference), len(rights_notes)) > 5000 or len(title) > 300:
        raise HttpError(422, 'A text field is too long.')
    if file.size and file.size > svc.max_asset_bytes():
        raise HttpError(413, 'Upload exceeds the asset file-size limit.')
    disk_check(settings.MEDIA_ROOT)
    if MediaAsset.objects.filter(owner=owner, deleted_at__isnull=True).count() >= svc.max_assets_per_owner():
        raise StudioError('asset_limit', f'Library limit of {svc.max_assets_per_owner()} assets reached. Delete unused assets first.')
    folder = svc.owner_dir(owner)
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder/f'.{uuid.uuid4().hex}.part'
    final = None
    try:
        import hashlib
        digest, size = hashlib.sha256(), 0
        with temp.open('wb') as out:
            for chunk in file.chunks():
                size += len(chunk)
                if size > svc.max_asset_bytes():
                    raise HttpError(413, 'Upload exceeds the asset file-size limit.')
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise StudioError('empty_file', 'The uploaded file is empty.')
        sha = digest.hexdigest()
        existing = MediaAsset.objects.filter(owner=owner, sha256=sha, deleted_at__isnull=True).first()
        if existing:
            temp.unlink(missing_ok=True)
            return 200, {**serialize(existing), 'deduplicated': True}
        info = svc.inspect_file(temp, kind)
        ext = info.get('ext') or svc.safe_ext(file.name, kind)
        final = folder/f'{uuid.uuid4()}{ext}'
        temp.replace(final)
        import mimetypes
        mime = info.get('mime') or mimetypes.guess_type(final.name)[0] or 'application/octet-stream'
        try:
            with transaction.atomic():
                asset = MediaAsset.objects.create(
                    owner=owner, kind=kind, title=(title.strip() or Path(file.name or '').stem)[:300], file=svc.relative(final),
                    mime_type=mime, size_bytes=size, sha256=sha, duration=info.get('duration'), width=info.get('width'),
                    height=info.get('height'), origin='upload', source_url=source_url, license_status=license_status,
                    license_reference=license_reference, attribution_text=attribution_text.strip(),
                    rights_notes=rights_notes, tags=tag_list)
        except IntegrityError:
            final.unlink(missing_ok=True)
            existing = MediaAsset.objects.filter(owner=owner, sha256=sha, deleted_at__isnull=True).first()
            if existing:
                return 200, {**serialize(existing), 'deduplicated': True}
            raise
        final = None
        return 201, {**serialize(asset), 'deduplicated': False}
    finally:
        temp.unlink(missing_ok=True)
        if final is not None:
            final.unlink(missing_ok=True)


@router.get('/v1/assets')
def list_assets(request, kind: Optional[str] = None, license_status: Optional[str] = None, tag: Optional[str] = None,
                q: Optional[str] = None, limit: int = 24, offset: int = 0):
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    qs = MediaAsset.objects.filter(owner=request.auth, deleted_at__isnull=True)
    if kind:
        qs = qs.filter(kind=kind)
    if license_status:
        qs = qs.filter(license_status=license_status)
    if tag or q:
        qs = qs.annotate(tags_text=Cast('tags', TextField()))
    if tag:
        import json
        qs = qs.filter(tags_text__contains=json.dumps(tag.strip().lower()))
    if q and q.strip():
        term = q.strip()[:100]
        qs = qs.filter(Q(title__icontains=term) | Q(attribution_text__icontains=term) | Q(tags_text__icontains=term))
    total = qs.count()
    page = with_usage(qs).order_by('-created_at')[offset:offset+limit]
    return {'total': total, 'limit': limit, 'offset': offset, 'items': [serialize_annotated(a) for a in page]}


@router.get('/v1/assets/{asset_id}')
def asset_detail(request, asset_id: uuid.UUID):
    return serialize(owned_asset(request, asset_id))


class AssetPatch(Schema):
    title: Optional[str] = None
    tags: Optional[list[str]] = None
    license_status: Optional[str] = None
    license_reference: Optional[str] = None
    attribution_text: Optional[str] = None
    rights_notes: Optional[str] = None
    source_url: Optional[str] = None
    pinned: Optional[bool] = None
    retention_until: Optional[datetime] = None


@router.patch('/v1/assets/{asset_id}')
def patch_asset(request, asset_id: uuid.UUID, data: AssetPatch):
    asset = owned_asset(request, asset_id)
    changes = data.model_dump(exclude_unset=True)
    for field in ('title', 'license_status', 'pinned'):
        if field in changes and changes[field] is None:
            raise StudioError('invalid_field', f'{field} cannot be null.')
    if 'tags' in changes:
        changes['tags'] = svc.normalize_tags(changes['tags'] or [])
    if 'source_url' in changes:
        changes['source_url'] = clean_source_url(changes['source_url'])
    for field in ('license_reference', 'attribution_text', 'rights_notes'):
        if field in changes:
            changes[field] = (changes[field] or '').strip() if field == 'attribution_text' else (changes[field] or '')
    if len(changes.get('title') or '') > 300 or any(len(changes.get(f) or '') > 5000 for f in ('license_reference', 'attribution_text', 'rights_notes')):
        raise HttpError(422, 'A text field is too long.')
    status = changes.get('license_status', asset.license_status)
    attribution = changes.get('attribution_text', asset.attribution_text)
    svc.check_rights(status, attribution)
    for field, value in changes.items():
        setattr(asset, field, value)
    asset.save(update_fields=list(changes) or None)
    return serialize(asset)


@router.get('/v1/assets/{asset_id}/file')
def asset_file(request, asset_id: uuid.UUID):
    asset = owned_asset(request, asset_id)
    response = private_file(asset.file, request=request)
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@router.get('/v1/assets/{asset_id}/thumbnail')
def asset_thumbnail(request, asset_id: uuid.UUID):
    asset = owned_asset(request, asset_id)
    try:
        rel = svc.ensure_thumbnail(asset)
    except StudioError:
        raise Http404('Asset expired or unavailable')
    if not rel:
        raise Http404('No thumbnail for this asset')
    return private_file(rel, request=request)


@router.delete('/v1/assets/{asset_id}', response={200: dict, 409: dict})
def delete_asset(request, asset_id: uuid.UUID, hard: bool = False):
    asset = owned_asset(request, asset_id, include_deleted=hard)
    if asset.scenes.exists():
        projects = svc.referencing_projects(asset)
        names = ', '.join(p['title'] or str(p['id']) for p in projects) or 'a storyboard'
        return 409, {'error': 'asset_in_use', 'message': f'This asset is used in: {names}. Remove it from those storyboards first.',
                     'projects': [{'id': str(p['id']), 'title': p['title']} for p in projects]}
    if hard:
        svc.remove_files(asset)
        asset.delete()
        return {'deleted': True, 'hard': True}
    asset.deleted_at = timezone.now()
    asset.save(update_fields=['deleted_at'])
    return {'deleted': True, 'hard': False}
