import shutil
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.conf import settings
from django.utils import timezone
from django.db.models import Q
from api.models import GenerationJob, GenerationProject, MediaAsset
from api.services import assets as asset_service
from api.tasks import TERMINAL


class Command(BaseCommand):
    help = 'Expire abandoned jobs, prune source footage and remove expired terminal outputs. Narration preview assets and story job folders follow the same retention. Schedule hourly (cron) or rely on the daily Celery beat task.'
    def handle(self, **options):
        now = timezone.now()
        stale = now-timedelta(seconds=settings.JOB_TIMEOUT+120)
        lost = GenerationJob.objects.exclude(status__in=TERMINAL).filter(Q(started_at__lt=stale) | Q(started_at__isnull=True, created_at__lt=stale)).update(
            status='failed', stage='Worker interrupted', error_code='worker_lost',
            error_message='The worker stopped or timed out. Start the worker and retry.', completed_at=now)
        sources = GenerationJob.objects.filter(status__in=TERMINAL,
            created_at__lt=now-timedelta(days=settings.SOURCE_RETENTION_DAYS))
        for job in sources.iterator():
            shutil.rmtree(settings.MEDIA_ROOT/'jobs'/str(job.pk)/'source', ignore_errors=True)
        jobs = GenerationJob.objects.filter(status__in=TERMINAL,
            created_at__lt=now-timedelta(days=settings.OUTPUT_RETENTION_DAYS))
        count = 0
        for job in jobs.iterator():
            if job.shorts.filter(posts__status__in=['draft','queued','uploading','failed']).exists():
                continue
            root = settings.MEDIA_ROOT/'jobs'/str(job.pk)
            shutil.rmtree(root, ignore_errors=True)
            source = job.source          # story jobs may have no SourceVideo
            job.delete()
            if source is not None and not source.generationjob_set.exists():
                source.delete()
            count += 1
        expired = purged = 0
        for asset in MediaAsset.objects.filter(retention_until__lt=now, pinned=False, deleted_at__isnull=True).iterator():
            if asset.scenes.exists():
                continue
            asset_service.remove_files(asset)
            asset.delete()
            expired += 1
        for asset in MediaAsset.objects.filter(deleted_at__lt=now-timedelta(days=7)).iterator():
            if asset.scenes.exists():
                continue
            asset_service.remove_files(asset)
            asset.delete()
            purged += 1
        # Projects emptied by job expiry. Skip fresh ones and any with story content (concepts/scripts).
        empty = GenerationProject.objects.filter(jobs__isnull=True, outputs__isnull=True, concepts__isnull=True,
            scripts__isnull=True, created_at__lt=now-timedelta(days=1))
        projects = empty.count()
        GenerationProject.objects.filter(pk__in=list(empty.values_list('pk', flat=True))).delete()
        self.stdout.write(f'Removed {count} expired jobs; marked {lost} interrupted jobs failed; expired {expired} assets, '
                          f'purged {purged} deleted assets, removed {projects} empty projects.')
