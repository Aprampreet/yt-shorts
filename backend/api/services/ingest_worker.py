"""Short-lived downloader subprocess; invoked only with server-validated arguments."""
import json
import os
import sys
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
from .media import download_sync, StudioError  # noqa: E402

if __name__ == '__main__':
    try:
        path, metadata = download_sync(sys.argv[1], Path(sys.argv[2]))
        payload = {'filename': path.name, 'metadata': metadata}
    except StudioError as exc:
        payload = {'error': exc.code, 'message': exc.message}
    print(json.dumps(payload))
