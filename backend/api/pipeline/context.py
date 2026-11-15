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

    def attach_providers(self):
        """``ctx.provider`` stays the Hugging Face provider (same analyze/translate/commentary/transcribe API),
        now built by the registry so its calls pass the policy gate and land in the usage ledger."""
        from ..providers import registry
        self.provider = registry.get('huggingface', job=self.job)
        return self.provider

    def resolve(self, capability, **kwargs):
        """First usable provider for ``capability`` (policy-checked, ledger-recorded for this job)."""
        from ..providers import registry
        return registry.resolve(capability, job=self.job, **kwargs)

    def hosted(self, name):
        """A specific registered provider bound to this job (e.g. 'groq'); check ``usable`` before calling."""
        from ..providers import registry
        return registry.get(name, job=self.job)

    def usable(self, name, capability):
        from ..providers import registry
        return registry.usable(name, capability, job=self.job)

    def inference_calls(self):
        """Hosted inference requests recorded in the ledger for this job (what job.analysis reports)."""
        from ..providers import ledger
        return ledger.job_calls(self.job)

    def mark(self, name, before):
        self.timings[name] = round(time.monotonic()-before, 2)
        if time.monotonic()-self.started > settings.JOB_TIMEOUT:
            raise StudioError('job_timeout', 'Job exceeded its processing time budget.')
        disk_check(settings.MEDIA_ROOT)
