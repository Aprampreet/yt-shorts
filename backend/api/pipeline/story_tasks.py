"""Celery tasks for Story Studio (registered through settings.CELERY_IMPORTS; names prefixed ``api.story.``).

``dispatch(job)`` enqueues the task for ``job.step``. Tests patch ``api.pipeline.story_tasks.dispatch``.
"""
import logging

from celery import shared_task
from django.conf import settings

from . import story

log = logging.getLogger(__name__)


@shared_task(name='api.story.analyze')
def analyze_task(job_id):
    story.run_job(job_id)


@shared_task(name='api.story.write', soft_time_limit=settings.AI_TASK_SOFT_TIME_LIMIT)
def write_task(job_id):
    story.run_job(job_id)


@shared_task(name='api.story.render')
def render_task(job_id):
    story.run_job(job_id)


TASKS = {'analyze': analyze_task, 'write': write_task, 'render': render_task}


def dispatch(job):
    try:
        TASKS[job.step].delay(str(job.pk))
    except Exception:
        log.exception('Could not enqueue story job %s', job.pk)
        job.status, job.stage = 'failed', 'Worker unavailable'
        job.error_code = 'queue_unavailable'
        job.error_message = 'Could not reach Redis. Start Redis and the Celery worker, then retry.'
        job.save(update_fields=['status', 'stage', 'error_code', 'error_message'])
