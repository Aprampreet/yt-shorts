"""Acquire the source media (upload / retained / download), probe it and sample signals."""
import time

from ..services.analysis import sample_signals
from ..services.media import StudioError, download, probe
from .context import update


def acquire_source(ctx):
    job, folders = ctx.job, ctx.folders
    update(job, 'downloading', 'Retrieving authorized source', 4)
    tick = time.monotonic()
    if job.source.source_type == 'upload':
        source = folders['source']/'upload.mp4'
        if not source.exists():
            raise StudioError('source_expired', 'Source upload expired. Upload the original file again.')
        metadata = job.source.source_metadata
    else:
        retained = [path for path in folders['source'].glob('source.*')
                    if path.suffix.lower() in {'.mp4', '.webm', '.mkv', '.mov'}]
        if len(retained) == 1:
            try:
                probe(retained[0])
                source, metadata = retained[0], job.source.source_metadata
            except StudioError:
                retained[0].unlink(missing_ok=True)
                source, metadata = download(job.source.source_url, folders['source'])
        else:
            source, metadata = download(job.source.source_url, folders['source'])
    ctx.source, ctx.metadata = source, metadata
    ctx.mark('download', tick)


def inspect_source(ctx):
    job, source, metadata = ctx.job, ctx.source, ctx.metadata
    update(job, 'preprocessing', 'Inspecting timing, scenes and silence', 12)
    tick = time.monotonic()
    info = probe(source)
    s = job.source
    for key in ['duration', 'fps', 'width', 'height']:
        setattr(s, key, info[key])
    s.title = metadata.get('title') or 'Uploaded source'
    s.source_metadata = metadata
    s.save()
    ctx.info = info
    ctx.signals = sample_signals(source, info['duration'], info['has_audio'])
    ctx.mark('preprocessing', tick)
