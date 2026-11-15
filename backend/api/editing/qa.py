"""Technical QA for rendered files.

``validate_output`` returns the legacy report keys (score, checks, flags, unverified, basis)
with the legacy ``checks`` set unchanged, because callers gate on ``all(checks.values())``.
The extended measurements (loudness, true peak, A/V drift, caption safe area, overlay
bounds, provenance) are reported beside them in ``extended_checks`` and as flags; they
lower the score and set ``qa_status='flagged'`` but never fail the render on their own.
"""
from __future__ import annotations

import json
import re

from ..services.media import probe, run
from .timeline import Timeline, provenance_summary

LOUDNESS_TOLERANCE = 2.0     # LU around the target
TRUE_PEAK_CEILING = -0.5     # dBTP after AAC encode (target -1.5, limiter at 0.95)
DRIFT_LIMIT = 0.1            # seconds between audio and video stream durations


def _stream_durations(path):
    data = json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', path])[0])
    out = {}
    for stream in data.get('streams', []):
        kind = stream.get('codec_type')
        if kind in ('video', 'audio') and kind not in out:
            try:
                out[kind] = float(stream.get('duration') or data['format'].get('duration'))
            except (TypeError, ValueError):
                pass
    return out


def _ebur128(text):
    summary = text[text.rfind('Summary:'):] if 'Summary:' in text else ''
    def grab(pattern):
        m = re.search(pattern, summary)
        return float(m[1]) if m else None
    return {'integrated_lufs': grab(r'I:\s+(-?[\d.]+|-inf) LUFS'), 'lra_lu': grab(r'LRA:\s+(-?[\d.]+) LU'),
            'true_peak_dbtp': grab(r'Peak:\s+(-?[\d.]+|-inf) dBFS')}


def validate_output(path, expected_duration, dimensions, audio, *, timeline: Timeline | None = None,
                    target_lufs: float | None = None):
    info = probe(path)
    checks = {'video_stream': True, 'dimensions': (info['width'], info['height']) == tuple(dimensions),
              'duration': abs(info['duration'] - expected_duration) < .6,
              'audio_stream': not audio or info['has_audio']}
    measure_audio = bool(audio and info['has_audio'])
    _, diagnostics = run(['ffmpeg', '-hide_banner', '-nostdin', '-nostats', '-i', path,
        '-vf', 'blackdetect=d=0.5:pix_th=0.10,freezedetect=n=-50dB:d=2',
        *(['-af', 'ebur128=peak=true:framelog=quiet,volumedetect'] if measure_audio else []), '-f', 'null', '-'])
    # Static scenes and dark footage can be intentional; flag for review rather than invent a repair.
    black = bool(re.search(r'black_start:', diagnostics))
    frozen = 'freeze_start:' in diagnostics
    peak = re.search(r'max_volume: (-?[\d.]+) dB', diagnostics)
    clipping = bool(peak and float(peak[1]) >= -.1)
    _, errors = run(['ffmpeg', '-v', 'error', '-nostdin', '-i', path, '-f', 'null', '-'])
    checks['full_decode'] = not errors.strip()
    flags = []
    if black:
        flags.append('Dark frames detected; review whether intentional.')
    if frozen:
        flags.append('Static or frozen section detected; review source.')
    if clipping:
        flags.append('Audio peaks need review.')

    target = target_lufs if target_lufs is not None else (timeline.loudness_lufs if timeline else -16.0)
    extended = {}
    loudness = _ebur128(diagnostics) if measure_audio else {'integrated_lufs': None, 'lra_lu': None, 'true_peak_dbtp': None}
    loudness['target_lufs'] = target
    if measure_audio:
        integrated, tp = loudness['integrated_lufs'], loudness['true_peak_dbtp']
        extended['loudness'] = integrated is not None and abs(integrated - target) <= LOUDNESS_TOLERANCE
        extended['true_peak'] = tp is not None and tp <= TRUE_PEAK_CEILING
        if not extended['loudness']:
            flags.append(f'Integrated loudness {integrated} LUFS is outside {target}±{LOUDNESS_TOLERANCE} LU.')
        if not extended['true_peak']:
            flags.append(f'True peak {tp} dBTP is above {TRUE_PEAK_CEILING} dBTP.')
    durations = _stream_durations(path)
    drift = abs(durations['video'] - durations['audio']) if 'video' in durations and 'audio' in durations else None
    if drift is not None:
        extended['av_drift'] = drift < DRIFT_LIMIT
        if not extended['av_drift']:
            flags.append(f'Audio and video stream durations differ by {drift:.3f}s.')

    captions_report = overlays_report = None
    provenance = {'statuses': {}, 'items': 0, 'review_required': False, 'review_reasons': [],
                  'rights_review_status': 'not_assessed', 'basis': 'No timeline supplied.'}
    if timeline is not None:
        W, H = timeline.canvas.width, timeline.canvas.height
        outside = [o.id for o in timeline.overlays if not o.rect.inside(W, H)]
        extended['overlays_in_canvas'] = not outside
        overlays_report = {'count': len(timeline.overlays), 'outside': outside}
        if timeline.captions:
            safe = timeline.captions.safe_rect
            caption_overlays = [o for o in timeline.overlays if o.kind == 'caption']
            unsafe = [o.id for o in caption_overlays if not o.rect.within(safe)]
            measured = bool(caption_overlays)
            extended['captions_in_safe_rect'] = safe.inside(W, H) and not unsafe
            captions_report = {'safe_rect': safe.model_dump(), 'overlays': len(caption_overlays), 'outside': unsafe,
                               'geometry_measured': measured}
            if unsafe:
                flags.append(f'{len(unsafe)} caption groups extend outside the caption safe area.')
        for name in ('overlays_in_canvas',):
            if not extended[name]:
                flags.append('An overlay extends outside the canvas.')
        provenance = provenance_summary(timeline.provenance)
        if provenance['review_required']:
            flags.append('Media with unknown or restricted licence status needs rights review.')

    unverified = ['Semantic coherence and final-word completeness', 'Caption accuracy and visual readability',
                  'Perceptual A/V synchronization', 'Ownership and monetization eligibility']
    if captions_report and not captions_report['geometry_measured']:
        unverified.append('Caption geometry inside the safe area (ASS layout is not measured)')
    score = max(0, 100 - 20*sum(not ok for ok in checks.values()) - 8*len(flags))
    passed = all(checks.values())
    qa_status = 'failed' if not passed else ('flagged' if flags else 'passed')
    return dict(score=score, checks=checks, flags=flags, unverified=unverified,
                basis='Technical checks and editorial heuristics; not a retention prediction.',
                extended_checks=extended, loudness=loudness, true_peak_dbtp=loudness['true_peak_dbtp'],
                av_drift=drift, stream_durations=durations, captions=captions_report, overlays=overlays_report,
                provenance=provenance, rights_review_status=provenance['rights_review_status'], qa_status=qa_status)
