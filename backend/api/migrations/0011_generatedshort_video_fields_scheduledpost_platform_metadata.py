import django.db.models.deletion
from django.db import migrations, models


def link_shorts_to_projects(apps, schema_editor):
    """Give existing outputs the project their job got in 0009."""
    GeneratedShort = apps.get_model('api', 'GeneratedShort')
    GenerationJob = apps.get_model('api', 'GenerationJob')
    for job_id, project_id in GenerationJob.objects.exclude(project__isnull=True).values_list('pk', 'project_id').iterator():
        GeneratedShort.objects.filter(job_id=job_id, project__isnull=True).update(project_id=project_id)


class Migration(migrations.Migration):
    dependencies = [('api', '0010_story_models')]

    operations = [
        migrations.AddField(
            model_name='generatedshort',
            name='kind',
            field=models.CharField(choices=[('highlight', 'Highlight'), ('story', 'Story')], default='highlight', max_length=20),
        ),
        migrations.AddField(
            model_name='generatedshort',
            name='project',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='outputs', to='api.generationproject'),
        ),
        migrations.AddField(
            model_name='generatedshort',
            name='script',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='videos', to='api.generatedscript'),
        ),
        migrations.AddField(
            model_name='generatedshort',
            name='timeline',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='generatedshort',
            name='timeline_version',
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='generatedshort',
            name='qa_status',
            field=models.CharField(blank=True, choices=[('', 'Not checked'), ('passed', 'Passed'), ('flagged', 'Flagged'), ('failed', 'Failed')], default='', max_length=20),
        ),
        migrations.AddField(
            model_name='scheduledpost',
            name='platform_metadata',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name='GeneratedVideo',
            fields=[],
            options={'proxy': True, 'indexes': [], 'constraints': []},
            bases=('api.generatedshort',),
        ),
        migrations.RunPython(link_shorts_to_projects, migrations.RunPython.noop),
    ]
