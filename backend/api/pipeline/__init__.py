"""Shared job runtime. WP1 provides ensure_project(); later packages add the stage modules."""


def ensure_project(job):
    """Idempotently attach a kind='shorts' GenerationProject to a Shorts job and return it.

    Safe to call repeatedly and concurrently: the job row is only updated while its project is still
    NULL, and a losing racer deletes its spare project. Existing outputs of the job without a project
    are linked too. Story jobs must be created with their project; calling this on one without a
    project raises ValueError.
    """
    from ..models import GeneratedShort, GenerationJob, GenerationProject  # lazy: keep states importable early

    if job.project_id:
        return job.project
    if (job.pipeline or 'shorts') != 'shorts':
        raise ValueError(f'ensure_project only creates shorts projects; job {job.pk} is pipeline={job.pipeline!r}')
    source = job.source if job.source_id else None
    project = GenerationProject.objects.create(
        owner=job.owner, kind='shorts', title=((source.title if source else '') or '')[:300],
        source=source, status='active')
    if GenerationJob.objects.filter(pk=job.pk, project__isnull=True).update(project=project):
        job.project = project
    else:
        project.delete()
        job.project_id = GenerationJob.objects.values_list('project_id', flat=True).get(pk=job.pk)
        job.project = GenerationProject.objects.get(pk=job.project_id)
    GeneratedShort.objects.filter(job_id=job.pk, project__isnull=True).update(project=job.project)
    return job.project
