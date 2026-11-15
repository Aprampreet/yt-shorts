"""Create one kind='shorts' GenerationProject per existing GenerationJob.

Reverse: detach jobs from shorts projects that contain only shorts jobs, then delete those
projects (jobs are never deleted by the reverse).
"""
from django.db import migrations

PROJECT_STATUS = {'completed': 'completed', 'needs_review': 'needs_review', 'failed': 'failed', 'cancelled': 'failed'}


def forwards(apps, schema_editor):
    GenerationJob = apps.get_model('api', 'GenerationJob')
    GenerationProject = apps.get_model('api', 'GenerationProject')
    jobs = GenerationJob.objects.filter(project__isnull=True, pipeline='shorts').select_related('source')
    for job in jobs.iterator():
        project = GenerationProject.objects.create(
            owner=job.owner, kind='shorts', title=((job.source.title if job.source_id else '') or '')[:300],
            source_id=job.source_id, status=PROJECT_STATUS.get(job.status, 'active'))
        # auto_now_add/auto_now ignore explicit values on create, so copy timestamps afterwards.
        GenerationProject.objects.filter(pk=project.pk).update(created_at=job.created_at,
                                                               updated_at=job.completed_at or job.created_at)
        GenerationJob.objects.filter(pk=job.pk).update(project=project)


def backwards(apps, schema_editor):
    GenerationJob = apps.get_model('api', 'GenerationJob')
    GenerationProject = apps.get_model('api', 'GenerationProject')
    mixed = GenerationJob.objects.exclude(pipeline='shorts').exclude(project__isnull=True).values('project_id')
    doomed = GenerationProject.objects.filter(kind='shorts').exclude(pk__in=mixed)
    ids = list(doomed.values_list('pk', flat=True))
    # Detach first: GenerationJob.project cascades, and the reverse must never delete jobs.
    GenerationJob.objects.filter(project_id__in=ids).update(project=None)
    GenerationProject.objects.filter(pk__in=ids).delete()


class Migration(migrations.Migration):
    dependencies = [('api', '0008_generationproject_job_fields')]

    operations = [migrations.RunPython(forwards, backwards)]
