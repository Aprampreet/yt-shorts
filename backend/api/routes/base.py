import logging
from django.conf import settings
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI
from ninja.errors import ValidationError
from ninja.security import APIKeyCookie
from ..models import GenerationJob, GeneratedShort
from ..services.media import StudioError

log = logging.getLogger(__name__)


class StudioSession(APIKeyCookie):
    param_name = settings.SESSION_COOKIE_NAME
    def authenticate(self, request, key):
        return request.session.get('studio_owner')


api = NinjaAPI(title='Shorts Studio', version='1.0.0', auth=StudioSession())


@api.exception_handler(StudioError)
def studio_error(request, exc):
    return api.create_response(request, {'error': exc.code, 'message': exc.message}, status=400)


def validation_message(errors):
    """Human sentence naming the first offending field and what is wrong with it."""
    first = errors[0] if errors else {}
    loc = [str(part) for part in first.get('loc', ()) if part not in ('body', 'query', 'path', 'form', 'data', 'file')]
    field = '.'.join(loc) or 'request'
    problem = str(first.get('msg') or 'is invalid').rstrip('.')
    if problem.startswith('Value error, '):
        problem = problem[len('Value error, '):]
    return f'Check "{field}": {problem}.'


@api.exception_handler(ValidationError)
def validation_error(request, exc):
    # `detail` keeps ninja's default shape (a list of error dicts); `error` and `message` are additive.
    return api.create_response(request, {'detail': exc.errors, 'error': 'validation_error',
                                         'message': validation_message(exc.errors)}, status=422)


@api.exception_handler(Exception)
def unexpected_error(request, exc):
    log.exception('Unhandled API error', exc_info=exc)
    return api.create_response(request, {'error':'internal_error',
        'message':'The studio could not complete this request. Check backend logs.'}, status=500)


def owned_job(request, job_id):
    return get_object_or_404(GenerationJob.objects.select_related('source'), pk=job_id, owner=request.auth)


def owned_short(request, short_id):
    return get_object_or_404(GeneratedShort.objects.select_related('job__source'), pk=short_id, job__owner=request.auth)
