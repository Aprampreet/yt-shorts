import os
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
env_file = BASE_DIR.parent / '.env'
if env_file.is_file():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, value = line.split('=', 1)
        name, value = name.strip(), value.strip()
        if not name.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not os.environ.get(name):
            os.environ[name] = value
DEBUG = os.getenv('DJANGO_DEBUG', 'false').lower() == 'true'
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'local-development-only-do-not-deploy'
    else:
        raise ImproperlyConfigured('Set DJANGO_SECRET_KEY (or DJANGO_DEBUG=true for local development).')
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1,testserver').split(',')
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
                  'corsheaders', 'api']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'api.middleware.UploadSizeLimit', 'corsheaders.middleware.CorsMiddleware',
              'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware',
              'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF = 'backend.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [], 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.debug',
              'django.template.context_processors.request', 'django.contrib.auth.context_processors.auth',
              'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'backend.wsgi.application'
LOCAL_STATE = BASE_DIR / '.local'
if not os.getenv('DATABASE_URL'):
    LOCAL_STATE.mkdir(parents=True, exist_ok=True)
DATABASES = {'default': dj_database_url.config(default=f'sqlite:///{LOCAL_STATE / "studio.sqlite3"}', conn_max_age=60)}
DATABASES['default'].setdefault('OPTIONS', {})
if DATABASES['default']['ENGINE'].endswith('sqlite3'):
    DATABASES['default']['OPTIONS']['timeout'] = 30
CORS_ALLOWED_ORIGINS = os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174').split(',')
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_ROOT = Path(os.getenv('MEDIA_ROOT', str(BASE_DIR / 'media')))
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
MAX_SOURCE_BYTES = int(os.getenv('MAX_SOURCE_BYTES', 500 * 1024 * 1024))
MAX_SOURCE_DURATION = int(os.getenv('MAX_SOURCE_DURATION', 1800))
MAX_DISK_BYTES = int(os.getenv('MAX_DISK_BYTES', 5 * 1024**3))
DOWNLOAD_TIMEOUT = int(os.getenv('DOWNLOAD_TIMEOUT', 600))
PROCESS_TIMEOUT = int(os.getenv('PROCESS_TIMEOUT', 600))
JOB_TIMEOUT = int(os.getenv('JOB_TIMEOUT', 3600))
SOURCE_RETENTION_DAYS = int(os.getenv('SOURCE_RETENTION_DAYS', 1))
OUTPUT_RETENTION_DAYS = int(os.getenv('OUTPUT_RETENTION_DAYS', 7))
CELERY_BROKER_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_TRACK_STARTED = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_CONCURRENCY = 1
CELERY_TASK_TIME_LIMIT = JOB_TIMEOUT
CELERY_TASK_SOFT_TIME_LIMIT = JOB_TIMEOUT - 15
CELERY_BROKER_CONNECTION_TIMEOUT = 3
CELERY_TASK_PUBLISH_RETRY = False
CELERY_BEAT_SCHEDULE = {'refresh-youtube-publications': {'task':'api.tasks.refresh_youtube_publications','schedule':300.0}}
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
LOGGING = {'version': 1, 'disable_existing_loggers': False,
           'handlers': {'console': {'class': 'logging.StreamHandler'}},
           'root': {'handlers': ['console'], 'level': 'INFO'}}
