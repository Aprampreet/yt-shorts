"""Render a Timeline to a file: compile, encode, QA, one bounded repair attempt."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..services.media import StudioError, run
from . import ffmpeg_graph, qa
from .timeline import Timeline

log = logging.getLogger(__name__)
CRITICAL = ('video_stream', 'dimensions', 'duration', 'audio_stream', 'full_decode')


@dataclass
class RenderResult:
    output: Path
    plan: ffmpeg_graph.RenderPlan
    report: dict
    attempts: int
    warnings: list = field(default_factory=list)


def render_timeline(timeline: Timeline, output, workdir, *, x264_preset: str = 'veryfast', crf: int = 21,
                    timeout: int | None = None, run_qa: bool = True) -> RenderResult:
    """Render ``timeline`` to ``output`` (written atomically via a ``.partial`` sibling).

    Attempt 1 uses the full graph (script file when supported). If FFmpeg fails or a
    critical QA check fails, attempt 2 recompiles in safe mode (inline graph, no zoompan,
    no xfade, fade-only overlays, one thread). A second failure raises ``StudioError``.
    """
    output, workdir = Path(output), Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + '.partial' + output.suffix)
    warnings: list[str] = []
    last_error: StudioError | None = None
    for attempt in (1, 2):
        safe = attempt == 2
        plan = ffmpeg_graph.compile(timeline, workdir, output=partial.resolve(), inline=safe or None,
                                    x264_preset=x264_preset, crf=crf, safe_mode=safe)
        warnings += [w for w in plan.warnings if w not in warnings]
        try:
            run(plan.argv, timeout=timeout, cwd=plan.cwd or str(workdir))
        except StudioError as exc:
            if exc.code == 'processing_timeout':
                raise
            last_error = exc
            log.warning('Timeline render attempt %s failed: %s', attempt, exc.code)
            continue
        report = qa.validate_output(partial, plan.duration, plan.dims, plan.has_audio, timeline=timeline) if run_qa \
            else {'checks': {}, 'flags': [], 'qa_status': 'not_run'}
        if all(report['checks'].get(k, True) for k in CRITICAL):
            partial.replace(output)
            if safe:
                warnings.append('Rendered with the safe repair profile; some motion effects were simplified.')
            report.setdefault('flags', [])
            report['render_attempts'] = attempt
            report['render_warnings'] = warnings
            return RenderResult(output, plan, report, attempt, warnings)
        last_error = StudioError('output_invalid', 'Rendered output failed technical validation.')
        log.warning('Timeline render attempt %s failed QA: %s', attempt, report['checks'])
    partial.unlink(missing_ok=True)
    raise StudioError('output_invalid', 'Rendered output failed technical validation after one repair attempt.') \
        from last_error
