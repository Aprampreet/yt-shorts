"""Offline speech recognition from the authorized source audio."""
import json
import os
import shutil
from pathlib import Path

from django.conf import settings

from .media import StudioError, run


def parse_whisper_json(path, duration):
    try:
        data = json.loads(Path(path).read_text())
        language = data.get('result', {}).get('language')
        cues = []
        for segment in data['transcription']:
            offsets = segment['offsets']
            start = max(0.0, float(offsets['from']) / 1000)
            end = min(float(duration), float(offsets['to']) / 1000)
            spoken = segment['text'].strip()
            if spoken and end > start:
                cues.append({'start': start, 'end': end, 'text': spoken,
                             'timing': 'local_whisper', 'detected_language': language})
        return cues
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise StudioError('transcription_invalid', 'Local speech recognition returned invalid timestamps or text.') from exc


def transcribe_source(source, folder, duration):
    binary = os.getenv('LOCAL_WHISPER_BIN') or shutil.which('whisper-cli')
    model = Path(os.getenv('LOCAL_WHISPER_MODEL') or
                 (settings.BASE_DIR / '.local' / 'models' / 'ggml-base-q5_1.bin'))
    if not binary or not model.is_file():
        raise StudioError('local_transcription_unconfigured',
                          'Local transcription needs whisper-cli and a multilingual model. Set LOCAL_WHISPER_BIN and LOCAL_WHISPER_MODEL on the worker.')
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / 'speech.wav'
    output = folder / 'speech-transcript'
    result = output.with_suffix('.json')
    try:
        run(['ffmpeg', '-nostdin', '-y', '-i', source, '-vn', '-ac', '1', '-ar', '16000',
             '-c:a', 'pcm_s16le', audio])
        try:
            run([binary, '-ng', '-m', model, '-f', audio, '-l', 'auto', '-oj', '-of', output,
                 '-t', min(4, os.cpu_count() or 2), '-np'], timeout=settings.PROCESS_TIMEOUT)
        except StudioError as exc:
            raise StudioError('transcription_failed',
                              'Local speech recognition failed. Check the worker log and model, then retry.') from exc
        return parse_whisper_json(result, duration)
    finally:
        audio.unlink(missing_ok=True)
        result.unlink(missing_ok=True)
