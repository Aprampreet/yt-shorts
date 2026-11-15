import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('api', '0006_scheduledpost_approval_basis')]

    operations = [
        migrations.CreateModel(
            name='MediaAsset',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('owner', models.CharField(db_index=True, max_length=64)),
                ('kind', models.CharField(choices=[('video', 'Video'), ('audio', 'Audio'), ('image', 'Image'), ('music', 'Music'), ('sfx', 'Sfx'), ('narration', 'Narration'), ('graphic', 'Graphic'), ('subtitle', 'Subtitle')], max_length=20)),
                ('title', models.CharField(blank=True, max_length=300)),
                ('file', models.CharField(max_length=500)),
                ('mime_type', models.CharField(blank=True, max_length=100)),
                ('size_bytes', models.BigIntegerField(default=0)),
                ('sha256', models.CharField(blank=True, db_index=True, max_length=64)),
                ('duration', models.FloatField(blank=True, null=True)),
                ('width', models.PositiveIntegerField(blank=True, null=True)),
                ('height', models.PositiveIntegerField(blank=True, null=True)),
                ('origin', models.CharField(choices=[('upload', 'Upload'), ('manual_narration', 'Manual narration'), ('generated_tts', 'Generated tts'), ('generated_graphic', 'Generated graphic'), ('generated_render', 'Generated render'), ('source_ingest', 'Source ingest')], default='upload', max_length=30)),
                ('source_url', models.URLField(blank=True)),
                ('license_status', models.CharField(choices=[('owned', 'Owned'), ('commercial_use_permitted', 'Commercial use permitted'), ('attribution_required', 'Attribution required'), ('permission_pending', 'Permission pending'), ('unknown', 'Unknown'), ('restricted', 'Restricted')], default='unknown', max_length=30)),
                ('license_reference', models.TextField(blank=True)),
                ('attribution_text', models.TextField(blank=True)),
                ('rights_notes', models.TextField(blank=True)),
                ('provider', models.CharField(blank=True, max_length=60)),
                ('tags', models.JSONField(blank=True, default=list)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('retention_until', models.DateTimeField(blank=True, null=True)),
                ('pinned', models.BooleanField(default=False)),
                ('deleted_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('source_video', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='media_assets', to='api.sourcevideo')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['owner', 'kind'], name='asset_owner_kind'),
                            models.Index(fields=['owner', 'license_status'], name='asset_owner_license')],
                'constraints': [
                    models.CheckConstraint(condition=models.Q(('license_status', 'attribution_required'), _negated=True) | models.Q(('attribution_text', ''), _negated=True), name='asset_attribution_text_required'),
                    models.UniqueConstraint(condition=models.Q(('deleted_at__isnull', True), models.Q(('sha256', ''), _negated=True)), fields=('owner', 'sha256'), name='unique_live_asset_per_owner'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ProviderUsage',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('owner', models.CharField(blank=True, max_length=64)),
                ('provider', models.CharField(max_length=60)),
                ('capability', models.CharField(max_length=20)),
                ('model', models.CharField(blank=True, max_length=120)),
                ('billing', models.CharField(choices=[('local_free', 'local_free'), ('free_tier', 'free_tier'), ('metered_unknown', 'metered_unknown'), ('paid', 'paid')], max_length=20)),
                ('units', models.FloatField(default=0)),
                ('unit_type', models.CharField(choices=[('calls', 'calls'), ('audio_seconds', 'audio_seconds'), ('characters', 'characters'), ('tokens', 'tokens')], default='calls', max_length=20)),
                ('cost_usd_estimate', models.DecimalField(blank=True, decimal_places=4, max_digits=10, null=True)),
                ('status', models.CharField(choices=[('ok', 'ok'), ('error', 'error'), ('rate_limited', 'rate_limited'), ('blocked_by_policy', 'blocked_by_policy'), ('invalid_response', 'invalid_response')], default='ok', max_length=20)),
                ('http_status', models.SmallIntegerField(blank=True, null=True)),
                ('latency_ms', models.IntegerField(blank=True, null=True)),
                ('retry_after_until', models.DateTimeField(blank=True, null=True)),
                ('job', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='provider_usage', to='api.generationjob')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['provider', 'created_at'], name='usage_provider_created'),
                            models.Index(fields=['billing', 'created_at'], name='usage_billing_created')],
            },
        ),
    ]
