import uuid
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError
from pydantic import Field
from ..models import TranscriptSegment
from ..services.media import StudioError
from ..tasks import rerender_short
from .base import owned_short
from .serializers import short_data

router = Router()


@router.get('/v1/shorts/{short_id}')
@router.get('/v1/shorts/{short_id}/metadata')
def short_metadata(request, short_id: uuid.UUID):
    return short_data(owned_short(request, short_id))


@router.post('/v1/shorts/{short_id}/metadata')
def refresh_metadata(request, short_id: uuid.UUID):
    from ..services.providers import HuggingFaceProvider
    from ..services.groq import finalize_description, generate_metadata
    short = owned_short(request, short_id)
    if not short.transcript:
        raise StudioError('transcript_required', 'No transcript is available for grounded metadata generation.')
    data = generate_metadata(short.transcript)
    if data:
        short.title = data['title']
        short.description = finalize_description(data['description'], data['hashtags'])
        short.hashtags = data['hashtags']
        short.quality_report = dict(short.quality_report, title_candidates=[short.title], metadata_source='groq')
    else:
        data = HuggingFaceProvider().analyze(short.transcript)
        short.title = data['title_candidates'][0][:500]
        short.description = finalize_description(data['description'], data['hashtags'])
        short.hashtags = data['hashtags']
        short.quality_report = dict(short.quality_report, title_candidates=data['title_candidates'])
    short.save()
    return short_data(short)


class SegmentEdit(Schema):
    id: uuid.UUID
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    original_text: str = Field(min_length=1, max_length=1000)
    translated_english: str = Field(default='', max_length=1000)
    translated_hindi: str = Field(default='', max_length=1000)


class TranscriptEdit(Schema):
    segments: list[SegmentEdit] = Field(min_length=1, max_length=500)


@router.put('/v1/shorts/{short_id}/transcript', response={202: dict})
def edit_transcript(request, short_id: uuid.UUID, data: TranscriptEdit):
    short = owned_short(request, short_id)
    source_folder = settings.MEDIA_ROOT/'jobs'/str(short.job_id)/'source'
    if not source_folder.exists():
        raise HttpError(410,'Original source expired; edited captions cannot be burned into the video.')
    existing = list(short.segments.all())
    if len(existing) != len(data.segments) or {s.id for s in existing} != {s.id for s in data.segments}:
        raise HttpError(422,'Edit existing segments only; IDs and segment count must match.')
    sorted_items = sorted(data.segments,key=lambda s:s.start_ms)
    for index, segment in enumerate(sorted_items):
        if segment.end_ms <= segment.start_ms or segment.end_ms > short.duration*1000+100 or (index and segment.start_ms < sorted_items[index-1].end_ms):
            raise HttpError(422,'Segments must be ordered, non-overlapping and within this Short.')
    with transaction.atomic():
        for segment in data.segments:
            TranscriptSegment.objects.filter(short=short,pk=segment.id).update(
                start_ms=segment.start_ms,end_ms=segment.end_ms,original_text=segment.original_text.strip(),
                translated_english=segment.translated_english.strip(),translated_hindi=segment.translated_hindi.strip())
        short.quality_report = dict(short.quality_report,transcript_edit_status='rendering')
        short.save(update_fields=['quality_report'])
    rerender_short.delay(str(short.id))
    return 202, short_data(short)


@router.post('/v1/shorts/{short_id}/rights-review')
def approve_rights(request, short_id: uuid.UUID, permission_verified: bool = False, music_verified: bool = False):
    short = owned_short(request,short_id)
    if not permission_verified or not music_verified:
        raise HttpError(422,'Confirm source permission and music rights separately after reviewing the final video.')
    short.rights_review_status = 'rights_documented'
    short.quality_report = dict(short.quality_report,rights_review=dict(short.quality_report.get('rights_review',{}),
        manual_source_permission_review=True,manual_music_review=True,reviewed_at=timezone.now().isoformat(),
        copyright_permission='User-documented permission; not independently verified'))
    short.save(update_fields=['rights_review_status','quality_report'])
    return short_data(short)
