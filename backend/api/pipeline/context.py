"""Shared job runtime: the JobContext every Shorts pipeline stage receives, plus update()/Cancelled."""
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings

from ..models import GenerationJob
from ..services.media import StudioError, disk_check

FOLDER_NAMES = ['source', 'audio', 'captions', 'renders', 'thumbnails', 'reports']


class Cancelled(Exception):
    pass


def update(job, status, stage, progress):
    if not GenerationJob.objects.filter(pk=job.pk).exclude(status='cancelled').update(
            status=status, stage=stage, progress=progress):
        raise Cancelled()
    job.status, job.stage, job.progress = status, stage, progress


def relative(path):
    return str(path.relative_to(settings.MEDIA_ROOT))


@dataclass
class JobContext:
    job: Any
    root: Path
    folders: dict
    started: float
    timings: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    provider: Any = None
    source: Path | None = None      # path of the source media file
    metadata: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)
    signals: Any = None
    cues: list = field(default_factory=list)
    pool: list = field(default_factory=list)
    selected: list = field(default_factory=list)

    @classmethod
    def create(cls, job):
        root = settings.MEDIA_ROOT / 'jobs' / str(job.id)
        folders = {name: root/name for name in FOLDER_NAMES}
        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        return cls(job=job, root=root, folders=folders, started=time.monotonic())

    def mark(self, name, before):
        self.timings[name] = round(time.monotonic()-before, 2)
        if time.monotonic()-self.started > settings.JOB_TIMEOUT:
            raise StudioError('job_timeout', 'Job exceeded its processing time budget.')
        disk_check(settings.MEDIA_ROOT)
