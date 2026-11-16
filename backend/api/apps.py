import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


def watch_env_file(sender, **kwargs):
    """autoreload_started handler: restart the dev server when .env changes."""
    from django.conf import settings
    env_file = getattr(settings, 'ENV_FILE', None)
    if env_file:
        sender.watch_file(env_file)


class ApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'api'

    def ready(self):
        # The signal only fires under runserver's autoreloader; harmless elsewhere.
        from django.utils.autoreload import autoreload_started
        autoreload_started.connect(watch_env_file, dispatch_uid='jwd-watch-env-file')
