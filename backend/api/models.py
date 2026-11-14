import uuid
from django.db import models


class SourceVideo(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_url = models.URLField(blank=True)
    source_type = models.CharField(max_length=20, default='youtube')
    title = models.CharField(max_length=500, blank=True)
    duration = models.FloatField(default=0)
    fps = models.FloatField(default=0)
    width = models.PositiveIntegerField(default=0)
    height = models.PositiveIntegerField(default=0)
    license_status = models.CharField(max_length=40, default='verification_needed')
    rights_declaration = models.BooleanField(default=False)
    permission_reference = models.TextField(blank=True)
    source_metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class GenerationJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.ForeignKey(SourceVideo, on_delete=models.CASCADE)
    owner = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=30, default='queued', db_index=True)
    stage = models.CharField(max_length=100, default='Waiting for worker')
    progress = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=50, blank=True)
    error_message = models.TextField(blank=True)
    settings = models.JSONField(default=dict)
    analysis = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-created_at']


class GeneratedShort(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(GenerationJob, on_delete=models.CASCADE, related_name='shorts')
    short_number = models.PositiveSmallIntegerField()
    start_time = models.FloatField()
    end_time = models.FloatField()
    duration = models.FloatField()
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    hashtags = models.JSONField(default=list)
    transcript = models.TextField(blank=True)
    caption_file = models.CharField(max_length=500, blank=True)
    video_file = models.CharField(max_length=500)
    thumbnail_file = models.CharField(max_length=500, blank=True)
    quality_score = models.FloatField(default=0)
    quality_report = models.JSONField(default=dict)
    rights_review_status = models.CharField(max_length=40, default='manual_review_required')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['short_number']
        constraints = [models.UniqueConstraint(fields=['job', 'short_number'], name='unique_job_short')]


class TranscriptSegment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    short = models.ForeignKey(GeneratedShort, on_delete=models.CASCADE, related_name='segments')
    start_ms = models.PositiveIntegerField()
    end_ms = models.PositiveIntegerField()
    original_text = models.TextField()
    translated_english = models.TextField(blank=True)
    translated_hindi = models.TextField(blank=True)
    detected_language = models.CharField(max_length=30, default='und')
    confidence = models.FloatField(null=True)

    class Meta:
        ordering = ['start_ms']
        constraints = [models.CheckConstraint(condition=models.Q(end_ms__gt=models.F('start_ms')), name='segment_positive_duration')]


class ConnectedSocialAccount(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.CharField(max_length=64, db_index=True)
    platform = models.CharField(max_length=20)
    platform_account_id = models.CharField(max_length=200)
    account_name = models.CharField(max_length=300)
    encrypted_credentials = models.TextField()
    token_expiry = models.DateTimeField(null=True)
    connection_status = models.CharField(max_length=30, default='connected')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner','platform','platform_account_id'], name='unique_social_account')]


class ScheduledPost(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.CharField(max_length=64, db_index=True)
    generated_short = models.ForeignKey(GeneratedShort, null=True, on_delete=models.SET_NULL, related_name='posts')
    social_account = models.ForeignKey(ConnectedSocialAccount, null=True, on_delete=models.SET_NULL)
    platform = models.CharField(max_length=20)
    title = models.CharField(max_length=500)
    description = models.TextField()
    hashtags = models.JSONField(default=list)
    scheduled_at = models.DateTimeField(null=True)
    timezone = models.CharField(max_length=60, default='Asia/Kolkata')
    status = models.CharField(max_length=30, default='draft', db_index=True)
    approved_at = models.DateTimeField(null=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    external_post_id = models.CharField(max_length=200, blank=True)
    published_url = models.URLField(blank=True)
    encrypted_upload_session = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['generated_short','social_account','platform'],
                                                name='unique_short_social_destination')]


def default_posting_slots():
    return ['12:00','17:00','21:00']


class PostingPreference(models.Model):
    owner = models.CharField(max_length=64, primary_key=True)
    timezone = models.CharField(max_length=60, default='Asia/Kolkata')
    slots = models.JSONField(default=default_posting_slots)
