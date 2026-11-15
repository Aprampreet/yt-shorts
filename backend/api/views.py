"""Compatibility shim: mounts every router on the single NinjaAPI and re-exports names tests/callers patch."""
from .routes.base import api
from .routes import system, jobs, files, transcripts, social
from .routes.jobs import disk_check  # noqa: F401
from .tasks import generate_job, rerender_short, publish_youtube_post  # noqa: F401

for _module in (system, jobs, files, transcripts, social):
    api.add_router('', _module.router)
