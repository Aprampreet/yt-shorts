import json
import mimetypes
import re
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Literal
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from ninja import Router
from ninja.errors import HttpError
from .base import owned_job, owned_short
from .serializers import short_data

router = Router()


def private_file(relative_path, attachment=False, filename=None, request=None):
    if not relative_path:
        raise Http404('Asset not available')
    root = settings.MEDIA_ROOT.resolve()
    path = (root/relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise Http404('Asset expired or unavailable')
    size = path.stat().st_size
    requested_range = request.headers.get('Range', '') if request and not attachment else ''
    if requested_range:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested_range)
        if not match or not any(match.groups()):
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(int(right) if right else size-1, size-1)
        else:
            start, end = max(0, size-int(right)), size-1
        if start > end or start >= size:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            return response
        def chunks():
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end-start+1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
        response = StreamingHttpResponse(chunks(), status=206,
            content_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        response['Content-Length'] = end-start+1
    else:
        response = FileResponse(path.open('rb'), as_attachment=attachment, filename=filename or path.name)
    response['Accept-Ranges'] = 'bytes'
    response['Cache-Control'] = 'private, no-store'
    return response


@router.get('/v1/shorts/{short_id}/video')
def video(request, short_id: uuid.UUID):
    return private_file(owned_short(request, short_id).video_file, request=request)


@router.get('/v1/shorts/{short_id}/download')
def download_short(request, short_id: uuid.UUID):
    short = owned_short(request, short_id)
    return private_file(short.video_file, True, f'short-{short.short_number}.mp4')


@router.get('/v1/shorts/{short_id}/thumbnail')
def thumbnail(request, short_id: uuid.UUID):
    return private_file(owned_short(request, short_id).thumbnail_file)


@router.get('/v1/shorts/{short_id}/captions')
def captions(request, short_id: uuid.UUID, format: Literal['srt','ass'] = 'srt'):
    short = owned_short(request, short_id)
    path = str(Path(short.caption_file).with_suffix('.'+format)) if short.caption_file else ''
    return private_file(path, True)


@router.get('/v1/jobs/{job_id}/download')
def download_all(request, job_id: uuid.UUID):
    job = owned_job(request, job_id)
    shorts = list(job.shorts.all())
    if not shorts:
        raise HttpError(409, 'No rendered Shorts available.')
    # Spools to disk instead of holding a potentially large ZIP in memory.
    archive = tempfile.TemporaryFile()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
        for short in shorts:
            for field, suffix in [('video_file', 'mp4'), ('caption_file', 'srt')]:
                value = getattr(short, field)
                if not value:
                    continue
                path = (settings.MEDIA_ROOT/value).resolve()
                if not path.is_relative_to(settings.MEDIA_ROOT.resolve()) or not path.is_file():
                    archive.close()
                    raise HttpError(410, 'An output expired. Regenerate this job.')
                output.write(path, f'short-{short.short_number}.{suffix}')
        output.writestr('metadata.json', json.dumps([short_data(s) for s in shorts], indent=2))
    archive.seek(0)
    return FileResponse(archive, as_attachment=True, filename=f'shorts-{job.pk}.zip')
