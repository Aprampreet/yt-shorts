"""Transcription: local whisper first, then bounded hosted chunks, then cue labelling.

``transcribe_source`` is called directly (it is what ``providers.local_whisper`` wraps, and tests patch
``api.pipeline.transcribe.transcribe_source``)."""
import time

from ..providers import Capability
from ..services.languages import label_cues
from ..services.local_transcription import transcribe_source
from ..services.media import StudioError, run
from .context import update


def transcribe(ctx):
    job, info, source, folders, warnings, provider = ctx.job, ctx.info, ctx.source, ctx.folders, ctx.warnings, ctx.provider
    update(job, 'transcribing', 'Transcribing source audio locally', 25)
    tick = time.monotonic()
    cues = []
    if info['has_audio']:
        try:
            cues = transcribe_source(source, folders['audio'], info['duration'])
            if cues:
                warnings.append('Locally generated speech captions need human accuracy review before publishing.')
        except StudioError as exc:
            warnings.append(exc.message)
    # Hosted fallback: the registry-built provider (policy gate + ledger inside request()). The configured
    # check is env-only, as before, so a policy block still surfaces its message as a warning.
    if not cues and info['has_audio'] and provider is not None and provider.availability(Capability.ASR).available:
        try:
            # Bounded 60-second mono chunks. Approximate timing is explicitly reported.
            for offset in range(0, int(info['duration'])+1, 60):
                if offset >= info['duration']:
                    break
                update(job, 'transcribing', f'Transcribing audio at {offset}s', 25)
                chunk_duration = min(60, info['duration']-offset)
                audio = folders['audio']/'chunk.flac'
                run(['ffmpeg', '-nostdin', '-y', '-ss', offset, '-i', source, '-t', chunk_duration,
                     '-vn', '-ac', '1', '-ar', '16000', audio])
                text, chunks = provider.transcribe(audio)
                if not chunks and text.strip():
                    chunks = [dict(start=0, end=chunk_duration, text=text, timing='approximate_chunk')]
                for cue in chunks:
                    a, b = max(0, cue['start']), min(chunk_duration, cue['end'])
                    if b > a:
                        cues.append(dict(cue, start=a+offset, end=b+offset))
        except StudioError as exc:
            warnings.append(exc.message)
    cues = label_cues(cues)
    if not cues:
        if info['has_audio'] and job.settings.get('require_captions', True):
            raise StudioError('transcription_unavailable',
                'No usable speech transcript was generated from the source audio. Check the local Whisper model and worker log, then retry.')
        warnings.append('No speech transcript available; this clip will have no spoken captions. Selection is heuristic.')
    ctx.cues = cues
    ctx.mark('transcription', tick)
