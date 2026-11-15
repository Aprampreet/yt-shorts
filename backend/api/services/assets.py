"""Media Library helpers: validation, storage, thumbnails and registration of generated media.

Rights are never defaulted optimistically: callers must pass a license_status explicitly
(the upload API defaults nothing; the model default is 'unknown')."""
import hashlib
import json
import math
import mimetypes
import uuid
from pathlib import Path
from django.conf import settings
from django.db import IntegrityError
from ..models import MediaAsset, LICENSE_STATUS_CHOICES, LICENSE_REVIEW_REQUIRED
from .media import StudioError, run

LICENSE_STATUSES = [k for k, _ in LICENSE_STATUS_CHOICES]
LICENSE_LABELS = dict(LICENSE_STATUS_CHOICES)
VIDEO_KINDS = {'video'}
AUDIO_KINDS = {'audio', 'music', 'sfx', 'narration'}
IMAGE_KINDS = {'image', 'graphic'}
UPLOAD_KINDS = VIDEO_KINDS | AUDIO_KINDS | IMAGE_KINDS
VIDEO_EXT = {'.mp4', '.mov', '.webm', '.mkv', '.m4v'}
AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.opus', '.flac'}
IMAGE_FORMATS = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp', 'GIF': '.gif'}
MAX_PIXELS = 8192
THUMB_SIZE = 320


def max_asset_bytes():
    return int(getattr(settings, 'MAX_ASSET_BYTES', settings.MAX_SOURCE_BYTES))


def max_assets_per_owner():
    return int(getattr(settings, 'MAX_ASSETS_PER_OWNER', 500))


def media_class(kind):
    if kind in VIDEO_KINDS:
        return 'video'
    if kind in AUDIO_KINDS:
        return 'audio'
    if kind in IMAGE_KINDS:
        return 'image'
    raise StudioError('invalid_kind', f'Assets of kind "{kind}" cannot be uploaded.')


def check_rights(license_status, attribution_text):
    if license_status not in LICENSE_STATUSES:
        raise StudioError('invalid_license', 'license_status must be one of: ' + ', '.join(LICENSE_STATUSES) + '.')
    if license_status == 'attribution_required' and not (attribution_text or '').strip():
        raise StudioError('attribution_required', 'Attribution text is required when the license status is "attribution required".')


def normalize_tags(tags):
    if isinstance(tags, str):
        tags = json.loads(tags) if tags.strip().startswith('[') else tags.split(',')
    if not isinstance(tags, list):
        raise StudioError('invalid_tags', 'Tags must be a list of short strings.')
    out = []
    for tag in tags:
        tag = str(tag).strip().lower()
        if tag and tag not in out:
            out.append(tag)
    if len(out) > 30 or any(len(t) > 40 for t in out):
        raise StudioError('invalid_tags', 'Use at most 30 tags of up to 40 characters.')
    return out


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def probe_av(path, need='video'):
    """ffprobe-based check. need='video' requires a video stream, 'audio' requires an audio stream."""
    try:
        data = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path])[0])
    except StudioError:
        raise StudioError('invalid_media', 'The file is not a playable media file of the declared kind.')
    except ValueError:
        raise StudioError('invalid_media', 'Unable to read media information.')
    streams = data.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video' and not (s.get('disposition') or {}).get('attached_pic')), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if need == 'video' and not video:
        raise StudioError('no_video', 'The file has no playable video stream.')
    if need == 'audio' and not audio:
        raise StudioError('no_audio', 'The file has no audio stream.')
    try:
        duration = float(data['format'].get('duration') or (video or audio).get('duration') or 0)
    except (ValueError, KeyError, TypeError):
        raise StudioError('invalid_media', 'Unable to read media timing.')
    if not math.isfinite(duration) or duration <= 0.1:
        raise StudioError('invalid_media', 'Media must have a valid duration.')
    if duration > settings.MAX_SOURCE_DURATION:
        raise StudioError('source_limit', 'Media exceeds the configured duration limit.')
    info = dict(duration=duration, width=None, height=None, has_audio=audio is not None)
    if video:
        w, h = int(video.get('width') or 0), int(video.get('height') or 0)
        if w <= 0 or h <= 0 or w > 4096 or h > 4096:
            raise StudioError('resolution_limit', 'Video resolution is invalid or exceeds the 4096-pixel limit.')
        info.update(width=w, height=h)
    return info


def probe_image(path):
    from PIL import Image
    try:
        with Image.open(path) as img:
            fmt = img.format
            img.verify()
        with Image.open(path) as img:
            width, height = img.size
    except Exception:
        raise StudioError('invalid_image', 'The file is not a valid image.')
    if fmt not in IMAGE_FORMATS:
        raise StudioError('invalid_image', 'Supported image formats: PNG, JPEG, WebP, GIF.')
    if not (0 < width <= MAX_PIXELS and 0 < height <= MAX_PIXELS):
        raise StudioError('resolution_limit', f'Image dimensions must be within {MAX_PIXELS} pixels.')
    from PIL import Image as _I
    return dict(duration=None, width=width, height=height, format=fmt,
                mime=_I.MIME.get(fmt, 'application/octet-stream'))


def inspect_file(path, kind):
    """Return dict(duration,width,height,mime,ext) for a file after content verification."""
    cls = media_class(kind)
    if cls == 'image':
        info = probe_image(path)
        info['ext'] = IMAGE_FORMATS[info['format']]
        return info
    info = probe_av(path, 'video' if cls == 'video' else 'audio')
    return info


def safe_ext(client_name, kind):
    cls = media_class(kind)
    ext = Path(client_name or '').suffix.lower()
    if cls == 'video':
        return ext if ext in VIDEO_EXT else '.mp4'
    return ext if ext in AUDIO_EXT else '.m4a'


def owner_dir(owner):
    return settings.MEDIA_ROOT/'assets'/str(owner)


def relative(path):
    return str(Path(path).resolve().relative_to(settings.MEDIA_ROOT.resolve()))


def asset_path(asset):
    """Absolute path of an asset's file, guaranteed to be inside MEDIA_ROOT and to exist."""
    root = settings.MEDIA_ROOT.resolve()
    path = (root/(asset.file or '')).resolve()
    if not asset.file or not path.is_relative_to(root):
        raise StudioError('asset_unavailable', 'Asset file path is invalid.')
    if not path.is_file():
        raise StudioError('asset_unavailable', 'Asset file is missing from storage.')
    return path


def thumb_relative(asset):
    return f'assets/{asset.owner}/thumbs/{asset.pk}.jpg'


def ensure_thumbnail(asset):
    """Create the thumbnail lazily; returns MEDIA_ROOT-relative path or None (audio / failure)."""
    cls = media_class(asset.kind) if asset.kind in UPLOAD_KINDS else None
    if cls not in ('video', 'image'):
        return None
    rel = thumb_relative(asset)
    target = settings.MEDIA_ROOT/rel
    if target.is_file():
        return rel
    source = asset_path(asset)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix('.tmp.jpg')
    try:
        if cls == 'image':
            from PIL import Image
            with Image.open(source) as img:
                img.seek(0)
                img = img.convert('RGB')
                img.thumbnail((THUMB_SIZE, THUMB_SIZE))
                img.save(tmp, 'JPEG', quality=80)
        else:
            at = min(1.0, (asset.duration or 0)/2)
            run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{at:.2f}', '-i', source, '-frames:v', '1',
                 '-vf', f'scale={THUMB_SIZE}:-2', '-q:v', '4', tmp])
        if not tmp.is_file():
            return None
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        return None
    return rel


def has_thumbnail(asset):
    return asset.kind in (VIDEO_KINDS | IMAGE_KINDS)


def remove_files(asset):
    root = settings.MEDIA_ROOT.resolve()
    for rel in (asset.file, thumb_relative(asset)):
        if not rel:
            continue
        path = (root/rel).resolve()
        if path.is_relative_to(root):
            path.unlink(missing_ok=True)


def referencing_projects(asset):
    """Titles/ids of projects whose storyboards use this asset."""
    from ..models import GenerationProject
    return list(GenerationProject.objects.filter(scripts__scenes__asset=asset).distinct().values('id', 'title'))


def register_generated_asset(owner, path, kind, origin, license_status, provider='', metadata=None, title=''):
    """Register an application-generated file (TTS narration, rendered graphic...) as a MediaAsset.

    `path` must be inside MEDIA_ROOT. license_status is mandatory and explicit. If the owner already
    has a live asset with identical content, that asset is returned instead of a duplicate row."""
    root = settings.MEDIA_ROOT.resolve()
    path = Path(path)
    resolved = (path if path.is_absolute() else root/path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise StudioError('asset_unavailable', 'Generated file must exist inside the media directory.')
    check_rights(license_status, '')
    if license_status == 'attribution_required':
        raise StudioError('attribution_required', 'Generated assets cannot use attribution_required without attribution text.')
    if origin not in dict(MediaAsset.ORIGIN_CHOICES):
        raise StudioError('invalid_origin', 'Unknown asset origin.')
    info = inspect_file(resolved, kind)
    digest = sha256_of(resolved)
    existing = MediaAsset.objects.filter(owner=owner, sha256=digest, deleted_at__isnull=True).first()
    if existing:
        return existing
    mime = info.get('mime') or mimetypes.guess_type(resolved.name)[0] or 'application/octet-stream'
    try:
        return MediaAsset.objects.create(
            owner=owner, kind=kind, title=title[:300] or resolved.stem[:300], file=str(resolved.relative_to(root)),
            mime_type=mime, size_bytes=resolved.stat().st_size, sha256=digest, duration=info.get('duration'),
            width=info.get('width'), height=info.get('height'), origin=origin, license_status=license_status,
            provider=provider, metadata=metadata or {})
    except IntegrityError:
        return MediaAsset.objects.get(owner=owner, sha256=digest, deleted_at__isnull=True)


def review_required(asset):
    return asset.license_status in LICENSE_REVIEW_REQUIRED
