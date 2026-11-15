import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('api', '0009_backfill_projects')]

    operations = [
        migrations.CreateModel(
            name='StoryConcept',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('index', models.PositiveSmallIntegerField()),
                ('angle', models.CharField(choices=[('explainer', 'Explainer'), ('timeline', 'Timeline'), ('myth_vs_fact', 'Myth vs fact'), ('top_list', 'Top list'), ('contrarian', 'Contrarian'), ('comparison', 'Comparison'), ('deep_dive', 'Deep dive'), ('story', 'Story')], max_length=30)),
                ('title', models.CharField(max_length=200)),
                ('logline', models.TextField(blank=True)),
                ('hook_idea', models.TextField(blank=True)),
                ('key_points', models.JSONField(blank=True, default=list)),
                ('claim_ids', models.JSONField(blank=True, default=list)),
                ('diversity_score', models.FloatField(default=0)),
                ('quality_score', models.FloatField(default=0)),
                ('selected', models.BooleanField(default=False)),
                ('revision', models.PositiveSmallIntegerField(default=1)),
                ('provider', models.CharField(blank=True, max_length=60)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='concepts', to='api.generationproject')),
            ],
            options={
                'ordering': ['project', 'index'],
                'constraints': [models.UniqueConstraint(fields=('project', 'index'), name='unique_project_concept_index')],
            },
        ),
        migrations.CreateModel(
            name='GeneratedScript',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('version', models.PositiveSmallIntegerField(default=1)),
                ('language', models.CharField(default='original', max_length=12)),
                ('narration_style', models.CharField(blank=True, max_length=30)),
                ('target_duration', models.PositiveSmallIntegerField(default=60)),
                ('sections', models.JSONField(blank=True, default=dict)),
                ('full_text', models.TextField(blank=True)),
                ('word_count', models.IntegerField(default=0)),
                ('estimated_duration', models.FloatField(default=0)),
                ('quality_report', models.JSONField(blank=True, default=dict)),
                ('originality_overlap', models.FloatField(default=0)),
                ('attempts', models.PositiveSmallIntegerField(default=1)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('approved', 'Approved'), ('needs_review', 'Needs review'), ('rejected', 'Rejected')], default='draft', max_length=20)),
                ('voice', models.JSONField(blank=True, default=dict)),
                ('provider', models.CharField(blank=True, max_length=60)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scripts', to='api.generationproject')),
                ('concept', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='scripts', to='api.storyconcept')),
                ('narration_asset', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='narrated_scripts', to='api.mediaasset')),
            ],
            options={
                'ordering': ['project', 'concept', 'version'],
                'constraints': [models.UniqueConstraint(fields=('concept', 'version'), name='unique_concept_script_version')],
            },
        ),
        migrations.CreateModel(
            name='StoryboardScene',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('order', models.PositiveSmallIntegerField()),
                ('start', models.FloatField()),
                ('end', models.FloatField()),
                ('narration_text', models.TextField(blank=True)),
                ('visual_type', models.CharField(choices=[('asset_video', 'Asset video'), ('asset_image', 'Asset image'), ('fact_card', 'Fact card'), ('comparison_card', 'Comparison card'), ('callout', 'Callout'), ('numbered_list', 'Numbered list'), ('title_card', 'Title card'), ('source_clip', 'Source clip')], max_length=30)),
                ('asset_in', models.FloatField(blank=True, null=True)),
                ('asset_out', models.FloatField(blank=True, null=True)),
                ('graphic_spec', models.JSONField(blank=True, default=dict)),
                ('transition', models.CharField(default='cut', max_length=20)),
                ('rights_status', models.CharField(default='unknown', max_length=30)),
                ('notes', models.TextField(blank=True)),
                ('script', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='scenes', to='api.generatedscript')),
                ('asset', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='scenes', to='api.mediaasset')),
            ],
            options={
                'ordering': ['script', 'order'],
                'constraints': [
                    models.UniqueConstraint(fields=('script', 'order'), name='unique_script_scene_order'),
                    models.CheckConstraint(condition=models.Q(('end__gt', models.F('start'))), name='scene_positive_duration'),
                ],
            },
        ),
    ]
