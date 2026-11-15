import logging
import os
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Literal
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from ninja import Router, Schema, File, Form
from ninja.errors import HttpError
from ninja.files import UploadedFile
from pydantic import Field
from ..models import SourceVideo, GenerationJob
from ..pipeline import ensure_project
from ..services.media import StudioError, validate_youtube, disk_check
from ..tasks import generate_job, TERMINAL
from .base import owned_job
from .serializers import job_data

log = logging.getLogger(__name__)
router = Router()


class JobOptions(Schema):
    caption_style: Literal['gaming', 'minimal', 'cinematic', 'dynamic'] = 'minimal'
    subtitle_mode: Literal['original', 'english', 'hindi', 'bilingual'] = 'original'
    require_captions: bool = True
    target_duration: int = Field(default=30, ge=20, le=60)
    resolution: Literal['720', '1080'] = '720'
    layout: Literal['preserve', 'center'] = 'preserve'
    mode: Literal['source_highlight','original_analysis'] = 'source_highlight'


class JobInput(Schema):
    url: str = Field(max_length=2048)
    rights_declaration: bool
    permission_reference: str = Field(default='', max_length=2000)
    options: JobOptions = Field(default_factory=JobOptions)


class Error(Schema):
    error: str
    message: str


def capacity(owner):
    disk_check(settings.MEDIA_ROOT)
    active = GenerationJob.objects.exclude(status__in=TERMINAL).count()
    if active >= 10 or GenerationJob.objects.filter(owner=owner).exclude(status__in=TERMINAL).count() >= 1:
        raise HttpError(429, 'A generation is already active or the queue is full. Please wait.')
    if GenerationJob.objects.filter(owner=owner, created_at__gte=timezone.now()-timedelta(hours=1)).count() >= 6:
        raise HttpError(429, 'Hourly generation limit reached. Try again later.')


def enqueue(job):
    try:
        generate_job.delay(str(job.pk))
    except Exception:
        log.exception('Could not enqueue job %s', job.pk)
        job.status = 'failed'
        job.stage = 'Worker unavailable'
        job.error_code = 'queue_unavailable'
        job.error_message = 'Could not reach Redis. Start Redis and the Celery worker, then retry.'
        job.save(update_fields=['status','stage','error_code','error_message'])


@router.post('/v1/jobs', response={202: dict})
@router.post('/generate-short', response={202: dict}, deprecated=True)
def create_job(request, data: JobInput):
    if data.options.mode == 'original_analysis' and not (os.getenv('AI_ALLOW_BILLABLE','false').lower() == 'true' and os.getenv('HF_TOKEN') and os.getenv('HF_TEXT_MODEL')):
        raise StudioError('commentary_unavailable','Original analysis requires an explicitly approved hosted text provider. Source Highlight remains available without it.')
    if not data.rights_declaration:
        raise StudioError('rights_required', 'Confirm ownership or permission, including commercial-use rights, before processing.')
    url = validate_youtube(data.url)
    capacity(request.auth)
    with transaction.atomic():
        source = SourceVideo.objects.create(source_url=url, rights_declaration=True,
            permission_reference=data.permission_reference, license_status='verification_needed')
        job = GenerationJob.objects.create(source=source, owner=request.auth, settings=data.options.model_dump())
        ensure_project(job)
    enqueue(job)
    return 202, job_data(job)


@router.post('/v1/uploads', response={202: dict})
def upload_job(request, file: File[UploadedFile], rights_declaration: Form[bool],
               permission_reference: Form[str] = '', options: Form[str] = '{}'):
    if not rights_declaration:
        raise StudioError('rights_required', 'Confirm ownership or permission before uploading.')
    if file.size > settings.MAX_SOURCE_BYTES:
        raise HttpError(413, 'Upload exceeds file-size limit.')
    if len(permission_reference) > 2000:
        raise HttpError(422, 'Permission reference is too long.')
    try:
        parsed = JobOptions.model_validate_json(options)
    except ValueError:
        raise HttpError(422, 'Invalid generation options.')
    if parsed.mode == 'original_analysis' and not (os.getenv('AI_ALLOW_BILLABLE','false').lower() == 'true' and os.getenv('HF_TOKEN') and os.getenv('HF_TEXT_MODEL')):
        raise StudioError('commentary_unavailable','Original analysis requires an explicitly approved hosted text provider.')
    capacity(request.auth)
    with transaction.atomic():
        source = SourceVideo.objects.create(source_type='upload', title=Path(file.name).name[:500],
            rights_declaration=True, permission_reference=permission_reference,
            source_metadata={'title': Path(file.name).name[:500]})
        job = GenerationJob.objects.create(source=source, owner=request.auth, settings=parsed.model_dump())
        ensure_project(job)
    folder = settings.MEDIA_ROOT/'jobs'/str(job.pk)/'source'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with (folder/'upload.mp4').open('wb') as output:
            size = 0
            for chunk in file.chunks():
                size += len(chunk)
                if size > settings.MAX_SOURCE_BYTES:
                    raise StudioError('source_limit', 'Upload exceeds file-size limit.')
                output.write(chunk)
    except Exception:
        shutil.rmtree(folder.parent, ignore_errors=True)
        job.delete()
        source.delete()
        raise
    enqueue(job)
    return 202, job_data(job)


@router.get('/v1/jobs')
def history(request):
    return [job_data(j) for j in GenerationJob.objects.filter(owner=request.auth).select_related('source').prefetch_related('shorts')[:30]]


@router.get('/v1/jobs/{job_id}')
@router.get('/v1/jobs/{job_id}/results')
def job_status(request, job_id: uuid.UUID):
    return job_data(owned_job(request, job_id))


@router.get('/v1/jobs/{job_id}/analysis')
def analysis_report(request, job_id: uuid.UUID):
    return owned_job(request, job_id).analysis


@router.post('/v1/jobs/{job_id}/cancel')
def cancel(request, job_id: uuid.UUID):
    job = owned_job(request, job_id)
    GenerationJob.objects.filter(pk=job.pk).exclude(status__in=TERMINAL).update(
        status='cancelled', stage='Cancelled', completed_at=timezone.now())
    job.refresh_from_db()
    return job_data(job)


@router.post('/v1/jobs/{job_id}/retry', response={202: dict})
def retry(request, job_id: uuid.UUID):
    job = owned_job(request, job_id)
    if job.status != 'failed':
        raise HttpError(409, 'Only failed jobs can be retried. Use regenerate for finished jobs.')
    capacity(request.auth)
    if GenerationJob.objects.filter(pk=job.pk, status='failed').update(status='queued', progress=0,
            stage='Waiting for worker', completed_at=None):
        job.shorts.all().delete()
        job.refresh_from_db()
        enqueue(job)
    return 202, job_data(job)


@router.post('/v1/jobs/{job_id}/regenerate', response={202: dict})
def regenerate(request, job_id: uuid.UUID):
    previous = owned_job(request, job_id)
    if previous.status not in TERMINAL:
        raise HttpError(409, 'Wait for the current job to finish.')
    capacity(request.auth)
    source = previous.source
    if source.source_type == 'upload':
        original = settings.MEDIA_ROOT/'jobs'/str(previous.pk)/'source'/'upload.mp4'
        if not original.exists():
            raise HttpError(410, 'Source expired. Upload it again.')
    job = GenerationJob.objects.create(source=source, owner=request.auth, settings=previous.settings)
    ensure_project(job)
    if source.source_type == 'upload':
        folder = settings.MEDIA_ROOT/'jobs'/str(job.pk)/'source'
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, folder/'upload.mp4')
    enqueue(job)
    return 202, job_data(job)
