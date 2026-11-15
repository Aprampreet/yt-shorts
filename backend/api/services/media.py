"""Bounded media processes. All paths are application-generated, never source titles."""
import json
import logging
import math
import os
import shutil
import re
import subprocess
import signal
import sys
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from django.conf import settings


class StudioError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def validate_youtube(url):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            raise ValueError()
        if p.hostname not in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'}:
            raise ValueError()
        q = parse_qs(p.query)
        if 'list' in q:
            raise ValueError()
        if p.hostname == 'youtu.be':
            identifier = p.path.lstrip('/')
        elif p.path == '/watch':
            identifier = q.get('v', [''])[0]
        elif p.path.startswith('/shorts/'):
            identifier = p.path.split('/')[2]
        else:
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
            raise ValueError()
        return f'https://www.youtube.com/watch?v={identifier}'
    except ValueError:
        raise StudioError('invalid_url', 'Use a public HTTPS YouTube video or Shorts URL, without a playlist.')


def run(args, timeout=None, cwd=None):
    try:
        args = list(args)
        if args[0] == 'ffmpeg':
            args[0] = os.getenv('FFMPEG_BIN') or (str(Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg')) if Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg').exists() else 'ffmpeg')
        if args[0] == 'ffprobe':
            args[0] = os.getenv('FFPROBE_BIN', 'ffprobe')
        process = subprocess.Popen([str(a) for a in args], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, cwd=cwd, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=timeout or settings.PROCESS_TIMEOUT)
        except BaseException:
            # Kill the entire media process group on timeout/cancellation, including
            # any FFmpeg child started by yt-dlp. Do not leave orphan encoders.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
    except FileNotFoundError:
        raise StudioError('missing_dependency', f'{args[0]} is required on the worker.')
    except subprocess.TimeoutExpired:
        raise StudioError('processing_timeout', 'Media processing exceeded its time limit. Try a shorter source.')
    if process.returncode:
        # Detailed diagnostics stay on worker logs, never include paths in API errors.
        import logging
        logging.getLogger(__name__).warning('Media command failed: %s', stderr[-3000:])
        raise StudioError('media_processing_failed', 'Media could not be processed. Check the source and worker logs.')
    return stdout, stderr


def probe(path):
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path])[0])
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if not video:
        raise StudioError('no_video', 'The source has no playable video stream.')
    try:
        fps = float(Fraction(video.get('avg_frame_rate', '0/1')))
        duration = float(data['format'].get('duration', video.get('duration', 0)))
    except (ValueError, ZeroDivisionError):
        raise StudioError('invalid_media', 'Unable to read source timing.')
    if not math.isfinite(duration) or duration < 3 or not math.isfinite(fps) or fps <= 0:
        raise StudioError('invalid_media', 'Video must have valid timing and be at least three seconds long.')
    if duration > settings.MAX_SOURCE_DURATION or Path(path).stat().st_size > settings.MAX_SOURCE_BYTES:
        raise StudioError('source_limit', 'Source exceeds the configured duration or file-size limit.')
    if video['width'] > 4096 or video['height'] > 4096:
        raise StudioError('resolution_limit', 'Source resolution exceeds the 4096-pixel limit.')
    return dict(duration=duration, fps=fps, width=video['width'], height=video['height'],
                has_audio=any(s['codec_type'] == 'audio' for s in data['streams']))


def disk_check(root):
    used = sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file()) if Path(root).exists() else 0
    if used > settings.MAX_DISK_BYTES:
        raise StudioError('disk_quota', 'Studio storage is full. Run cleanup or increase the disk quota.')
    return used


def sanitize_heatmap(raw, limit=200):
    """yt-dlp 'most replayed' buckets -> [{s, e, v}] finite floats, max ``limit``; [] when absent/invalid."""
    out = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            try:
                s = float(item.get('start_time', item.get('s')))
                e = float(item.get('end_time', item.get('e')))
                v = float(item.get('value', item.get('v')))
            except (AttributeError, TypeError, ValueError):
                continue
            if all(map(math.isfinite, (s, e, v))) and s >= 0 and e > s and v >= 0:
                out.append({'s': round(s, 3), 'e': round(e, 3), 'v': round(v, 5)})
    if len(out) > limit:
        out = [out[int(i * len(out) / limit)] for i in range(limit)]
    return out


def download_sync(url, folder):
    import yt_dlp
    url = validate_youtube(url)
    folder.mkdir(parents=True, exist_ok=True)
    def bounded(progress):
        if progress.get('downloaded_bytes', 0) > settings.MAX_SOURCE_BYTES:
            raise StudioError('source_limit', 'Download exceeded the file-size limit.')
    def permitted(info, *, incomplete=False):
        if info.get('duration', 0) > settings.MAX_SOURCE_DURATION:
            return 'Video exceeds duration limit'
        if info.get('is_live') or info.get('live_status') == 'is_live':
            return 'Live streams are not supported'
    options = {'format': 'bv*[height<=1080]+ba/b[height<=1080]', 'outtmpl': str(folder / 'source.%(ext)s'),
               'merge_output_format': 'mp4', 'noplaylist': True, 'quiet': True, 'socket_timeout': 30,
               'js_runtimes': {'node': {}},
               'retries': 2, 'fragment_retries': 2, 'max_filesize': settings.MAX_SOURCE_BYTES,
               'match_filter': permitted, 'progress_hooks': [bounded],
               'extractor_args': {'youtube': {'skip': ['hls', 'dash']}},
               'writethumbnail': False, 'writesubtitles': False, 'writeautomaticsub': False,
               'subtitlesformat': 'vtt', 'overwrites': True}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except StudioError:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning('YouTube download failed: %s', exc)
        detail = str(exc).lower()
        if any(s in detail for s in ('private video', 'members-only', 'sign in', 'login required',
                                      'age-restricted', 'not available in your country', 'drm protected')):
            raise StudioError('source_access_restricted',
                              'This video requires access that the studio does not have. Upload an authorized original file.') from exc
        if any(s in detail for s in ('timed out', 'temporary failure', 'unable to download api page',
                                      'connection error', 'http error 429')):
            raise StudioError('source_connection_failed',
                              'Could not reach YouTube or its video stream. Retry later, or upload an authorized original file.') from exc
        raise StudioError('source_unavailable',
                          'YouTube did not provide a downloadable stream. Check the worker log or upload an authorized original file.') from exc
    files = [p for p in folder.iterdir() if p.suffix.lower() in {'.mp4', '.webm', '.mkv', '.mov'}]
    if len(files) != 1:
        raise StudioError('download_incomplete', 'No complete video was downloaded. Upload the authorized source file instead.')
    metadata = {k: info.get(k) for k in ['id', 'title', 'description', 'uploader', 'channel', 'license', 'webpage_url', 'language']}
    metadata['heatmap'] = sanitize_heatmap(info.get('heatmap'))
    metadata['subtitle_tracks'] = list((info.get('subtitles') or {}).keys())
    metadata['automatic_caption_tracks'] = list((info.get('automatic_captions') or {}).keys())
    return files[0], metadata


def download(url, folder):
    """Isolate downloader + its FFmpeg children under a wall-clock deadline."""
    url = validate_youtube(url)
    folder.mkdir(parents=True, exist_ok=True)
    output, diagnostics = run([sys.executable, '-m', 'api.services.ingest_worker', url, folder.resolve()],
                              timeout=settings.DOWNLOAD_TIMEOUT, cwd=settings.BASE_DIR)
    try:
        payload = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise StudioError('download_incomplete', 'Downloader returned an incomplete result. Upload the original source instead.')
    if 'error' in payload:
        if diagnostics:
            logging.getLogger(__name__).warning('Downloader diagnostics: %s', diagnostics[-3000:])
        raise StudioError(payload['error'], payload['message'])
    path = (folder / payload['filename']).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise StudioError('download_incomplete', 'Downloaded source is unavailable.')
    return path, payload['metadata']
