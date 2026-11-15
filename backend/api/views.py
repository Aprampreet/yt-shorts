"""Compatibility shim: mounts every router on the single NinjaAPI and re-exports names tests/callers patch."""
from .routes.base import api
from .routes import system, jobs, files, transcripts, social
from .routes.jobs import disk_check  # noqa: F401
from .routes import assets as assets_routes
from .routes import providers as providers_routes
from .tasks import generate_job, rerender_short, publish_youtube_post  # noqa: F401

for _module in (system, jobs, files, transcripts, social):
    api.add_router('', _module.router)
api.add_router('', assets_routes.router)
api.add_router('', providers_routes.router)
from .routes import narration as narration_routes  # noqa: E402
api.add_router('', narration_routes.router)
from .routes import story as story_routes  # noqa: E402
api.add_router('', story_routes.router)
