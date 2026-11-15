import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('api', '0007_mediaasset_providerusage')]

    operations = [
        migrations.CreateModel(
            name='GenerationProject',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('owner', models.CharField(db_index=True, max_length=64)),
                ('kind', models.CharField(choices=[('shorts', 'Shorts'), ('story', 'Story')], max_length=20)),
                ('title', models.CharField(blank=True, max_length=300)),
                ('language', models.CharField(default='original', max_length=12)),
                ('inputs', models.JSONField(blank=True, default=dict)),
                ('source_notes', models.TextField(blank=True)),
                ('intelligence', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('active', 'Active'), ('needs_review', 'Needs review'), ('completed', 'Completed'), ('failed', 'Failed'), ('archived', 'Archived')], default='draft', max_length=30)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('source', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='projects', to='api.sourcevideo')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['owner', 'kind'], name='project_owner_kind')],
            },
        ),
        migrations.AddField(
            model_name='generationjob',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='jobs', to='api.generationproject'),
        ),
        migrations.AddField(
            model_name='generationjob',
            name='pipeline',
            field=models.CharField(choices=[('shorts', 'Shorts'), ('story', 'Story')], default='shorts', max_length=20),
        ),
        migrations.AddField(
            model_name='generationjob',
            name='step',
            field=models.CharField(blank=True, max_length=30),
        ),
        migrations.AlterField(
            model_name='generationjob',
            name='source',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='api.sourcevideo'),
        ),
    ]
