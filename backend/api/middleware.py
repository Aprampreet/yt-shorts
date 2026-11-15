from django.conf import settings
from django.http import JsonResponse


class UploadSizeLimit:
    """Reject oversized uploads before Django's multipart parser spools them to disk."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == 'POST' and request.path in ('/api/v1/uploads', '/api/v1/assets'):
            limit = settings.MAX_SOURCE_BYTES if request.path == '/api/v1/uploads' else int(getattr(settings, 'MAX_ASSET_BYTES', settings.MAX_SOURCE_BYTES))
            length = request.META.get('CONTENT_LENGTH', '')
            try:
                length = int(length)
            except (ValueError, TypeError):
                return JsonResponse({'error':'length_required','message':'Upload requires a Content-Length header.'}, status=411)
            if length < 0 or length > limit+1024*1024:
                return JsonResponse({'error':'upload_limit','message':'Upload exceeds the configured file-size limit.'}, status=413)
        return self.get_response(request)
