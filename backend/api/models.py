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


PIPELINE_CHOICES = [('shorts', 'Shorts'), ('story', 'Story')]


class GenerationProject(models.Model):
    """Thin umbrella over one or more GenerationJob runs (Shorts or Story)."""
    STATUS_CHOICES = [('draft', 'Draft'), ('active', 'Active'), ('needs_review', 'Needs review'),
                      ('completed', 'Completed'), ('failed', 'Failed'), ('archived', 'Archived')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=20, choices=PIPELINE_CHOICES)
    title = models.CharField(max_length=300, blank=True)
    language = models.CharField(max_length=12, default='original')  # en|hi|hinglish|original
    inputs = models.JSONField(default=dict, blank=True)
    source = models.ForeignKey(SourceVideo, null=True, blank=True, on_delete=models.SET_NULL, related_name='projects')
    source_notes = models.TextField(blank=True)  # user-supplied transcript/notes: untrusted data
    intelligence = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='draft')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['owner', 'kind'], name='project_owner_kind')]

    def __str__(self):
        return f'{self.kind}: {self.title or self.pk}'


class GenerationJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Nullable since 0008: story jobs may have no SourceVideo. Serializers must guard job.source.
    source = models.ForeignKey(SourceVideo, null=True, blank=True, on_delete=models.CASCADE)
    project = models.ForeignKey(GenerationProject, null=True, blank=True, on_delete=models.CASCADE, related_name='jobs')
    pipeline = models.CharField(max_length=20, choices=PIPELINE_CHOICES, default='shorts')
    step = models.CharField(max_length=30, blank=True)  # story: analyze|write|narrate|storyboard|render
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
    # Added in 0011. For kind='story', start_time/end_time are 0 and duration (no source span).
    kind = models.CharField(max_length=20, choices=[('highlight', 'Highlight'), ('story', 'Story')], default='highlight')
    project = models.ForeignKey(GenerationProject, null=True, blank=True, on_delete=models.CASCADE, related_name='outputs')
    script = models.ForeignKey('GeneratedScript', null=True, blank=True, on_delete=models.SET_NULL, related_name='videos')
    timeline = models.JSONField(default=dict, blank=True)
    timeline_version = models.PositiveSmallIntegerField(default=1)
    qa_status = models.CharField(max_length=20, blank=True, default='',
                                 choices=[('', 'Not checked'), ('passed', 'Passed'), ('flagged', 'Flagged'), ('failed', 'Failed')])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['short_number']
        constraints = [models.UniqueConstraint(fields=['job', 'short_number'], name='unique_job_short')]


class StoryVideoManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(kind='story')


class GeneratedVideo(GeneratedShort):
    """Story output: same table as GeneratedShort, restricted to kind='story'."""
    objects = StoryVideoManager()

    class Meta:
        proxy = True

    def save(self, *args, **kwargs):
        self.kind = 'story'
        super().save(*args, **kwargs)


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
    approval_basis = models.CharField(max_length=30, default='manual_review')
    retry_count = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    external_post_id = models.CharField(max_length=200, blank=True)
    published_url = models.URLField(blank=True)
    encrypted_upload_session = models.TextField(blank=True)
    # Per-platform caption, share_to_feed, cover, altered_content disclosure (added in 0011).
    platform_metadata = models.JSONField(default=dict, blank=True)
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


LICENSE_STATUS_CHOICES = [
    ('owned', 'Owned'),
    ('commercial_use_permitted', 'Commercial use permitted'),
    ('attribution_required', 'Attribution required'),
    ('permission_pending', 'Permission pending'),
    ('unknown', 'Unknown'),
    ('restricted', 'Restricted'),
]
# Statuses that force manual rights review (restricted additionally blocks story renders).
LICENSE_REVIEW_REQUIRED = frozenset({'unknown', 'permission_pending', 'restricted'})


class MediaAsset(models.Model):
    KIND_CHOICES = [(k, k.title()) for k in ('video', 'audio', 'image', 'music', 'sfx', 'narration', 'graphic', 'subtitle')]
    ORIGIN_CHOICES = [(k, k.replace('_', ' ').capitalize()) for k in (
        'upload', 'manual_narration', 'generated_tts', 'generated_graphic', 'generated_render', 'source_ingest')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    title = models.CharField(max_length=300, blank=True)
    file = models.CharField(max_length=500)  # MEDIA_ROOT-relative, like GeneratedShort.video_file
    mime_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    duration = models.FloatField(null=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    origin = models.CharField(max_length=30, choices=ORIGIN_CHOICES, default='upload')
    source_url = models.URLField(blank=True)
    source_video = models.ForeignKey(SourceVideo, null=True, blank=True, on_delete=models.SET_NULL, related_name='media_assets')
    license_status = models.CharField(max_length=30, choices=LICENSE_STATUS_CHOICES, default='unknown')
    license_reference = models.TextField(blank=True)
    attribution_text = models.TextField(blank=True)
    rights_notes = models.TextField(blank=True)
    provider = models.CharField(max_length=60, blank=True)
    tags = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    retention_until = models.DateTimeField(null=True, blank=True)
    pinned = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['owner', 'kind'], name='asset_owner_kind'),
                   models.Index(fields=['owner', 'license_status'], name='asset_owner_license')]
        constraints = [
            models.CheckConstraint(condition=~models.Q(license_status='attribution_required') | ~models.Q(attribution_text=''),
                                   name='asset_attribution_text_required'),
            models.UniqueConstraint(fields=['owner', 'sha256'], name='unique_live_asset_per_owner',
                                    condition=models.Q(deleted_at__isnull=True) & ~models.Q(sha256='')),
        ]

    def __str__(self):
        return self.title or str(self.pk)


class StoryConcept(models.Model):
    ANGLE_CHOICES = [(k, k.replace('_', ' ').capitalize()) for k in (
        'explainer', 'timeline', 'myth_vs_fact', 'top_list', 'contrarian', 'comparison', 'deep_dive', 'story')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(GenerationProject, on_delete=models.CASCADE, related_name='concepts')
    index = models.PositiveSmallIntegerField()
    angle = models.CharField(max_length=30, choices=ANGLE_CHOICES)
    title = models.CharField(max_length=200)
    logline = models.TextField(blank=True)
    hook_idea = models.TextField(blank=True)
    key_points = models.JSONField(default=list, blank=True)
    claim_ids = models.JSONField(default=list, blank=True)
    diversity_score = models.FloatField(default=0)
    quality_score = models.FloatField(default=0)
    selected = models.BooleanField(default=False)
    revision = models.PositiveSmallIntegerField(default=1)
    provider = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['project', 'index']
        constraints = [models.UniqueConstraint(fields=['project', 'index'], name='unique_project_concept_index')]


class GeneratedScript(models.Model):
    STATUS_CHOICES = [('draft', 'Draft'), ('approved', 'Approved'), ('needs_review', 'Needs review'), ('rejected', 'Rejected')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(GenerationProject, on_delete=models.CASCADE, related_name='scripts')
    # Nullable so a user-written (manual) script can exist without an LLM concept.
    concept = models.ForeignKey(StoryConcept, null=True, blank=True, on_delete=models.CASCADE, related_name='scripts')
    version = models.PositiveSmallIntegerField(default=1)
    language = models.CharField(max_length=12, default='original')
    narration_style = models.CharField(max_length=30, blank=True)
    target_duration = models.PositiveSmallIntegerField(default=60)
    sections = models.JSONField(default=dict, blank=True)  # ScriptSections{hook, context, core[], payoff, cta?}
    full_text = models.TextField(blank=True)
    word_count = models.IntegerField(default=0)
    estimated_duration = models.FloatField(default=0)
    quality_report = models.JSONField(default=dict, blank=True)
    originality_overlap = models.FloatField(default=0)
    attempts = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    narration_asset = models.ForeignKey(MediaAsset, null=True, blank=True, on_delete=models.SET_NULL, related_name='narrated_scripts')
    voice = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['project', 'concept', 'version']
        constraints = [models.UniqueConstraint(fields=['concept', 'version'], name='unique_concept_script_version')]


class StoryboardScene(models.Model):
    VISUAL_CHOICES = [(k, k.replace('_', ' ').capitalize()) for k in (
        'asset_video', 'asset_image', 'fact_card', 'comparison_card', 'callout', 'numbered_list', 'title_card', 'source_clip')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    script = models.ForeignKey(GeneratedScript, on_delete=models.CASCADE, related_name='scenes')
    order = models.PositiveSmallIntegerField()
    start = models.FloatField()  # narration time, seconds
    end = models.FloatField()
    narration_text = models.TextField(blank=True)
    visual_type = models.CharField(max_length=30, choices=VISUAL_CHOICES)
    # PROTECT: an asset used in a storyboard cannot be hard-deleted.
    asset = models.ForeignKey(MediaAsset, null=True, blank=True, on_delete=models.PROTECT, related_name='scenes')
    asset_in = models.FloatField(null=True, blank=True)
    asset_out = models.FloatField(null=True, blank=True)
    graphic_spec = models.JSONField(default=dict, blank=True)
    transition = models.CharField(max_length=20, default='cut')
    rights_status = models.CharField(max_length=30, default='unknown')  # snapshot; re-checked at render
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['script', 'order']
        constraints = [
            models.UniqueConstraint(fields=['script', 'order'], name='unique_script_scene_order'),
            models.CheckConstraint(condition=models.Q(end__gt=models.F('start')), name='scene_positive_duration'),
        ]


class ProviderUsage(models.Model):
    BILLING_CHOICES = [(k, k) for k in ('local_free', 'free_tier', 'metered_unknown', 'paid')]
    UNIT_CHOICES = [(k, k) for k in ('calls', 'audio_seconds', 'characters', 'tokens')]
    STATUS_CHOICES = [(k, k) for k in ('ok', 'error', 'rate_limited', 'blocked_by_policy', 'invalid_response')]
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    owner = models.CharField(max_length=64, blank=True)
    job = models.ForeignKey(GenerationJob, null=True, blank=True, on_delete=models.SET_NULL, related_name='provider_usage')
    provider = models.CharField(max_length=60)
    capability = models.CharField(max_length=20)
    model = models.CharField(max_length=120, blank=True)
    billing = models.CharField(max_length=20, choices=BILLING_CHOICES)
    units = models.FloatField(default=0)
    unit_type = models.CharField(max_length=20, choices=UNIT_CHOICES, default='calls')
    cost_usd_estimate = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='ok')
    http_status = models.SmallIntegerField(null=True, blank=True)
    latency_ms = models.IntegerField(null=True, blank=True)
    retry_after_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['provider', 'created_at'], name='usage_provider_created'),
                   models.Index(fields=['billing', 'created_at'], name='usage_billing_created')]
