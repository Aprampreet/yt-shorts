"""Legacy rendering entry points, now thin wrappers over the Timeline compiler.

``render_command`` and ``validate_output`` keep their original signatures and return
shapes; ``tasks.py`` still runs the returned argv itself with ``cwd`` = the caption folder.
"""
import os
from pathlib import Path

from .media import run, probe, StudioError  # noqa: F401  (re-exported for callers and patches)


def legacy_timeline(source, start, duration, info, resolution, layout, caption=None):
    """Express the legacy single-clip render as a Timeline. Returns (timeline, (width, height))."""
    from ..editing.timeline import Timeline
    width, height = (1080, 1920) if resolution == '1080' else (720, 1280)
    # Do not imply a low-resolution input supports a native 1080 output.
    if info['height'] < 1080 and resolution == '1080':
        width, height = 720, 1280
    fps = 60 if info['fps'] >= 50 and resolution == '1080' else min(30, info['fps'])
    source = Path(source) if os.path.isabs(source) else Path(os.path.abspath(source))
    roots = [str(source.parent)]
    media = dict(kind='source', path=str(source), duration=max(float(info.get('duration') or 0), start + duration),
                 has_audio=bool(info.get('has_audio')), width=info.get('width'), height=info.get('height'),
                 license_status=info.get('license_status', 'unknown'))
    crop = {'mode': 'center'} if layout == 'center' else {'mode': 'fit_blur'}
    data = dict(
        canvas=dict(width=width, height=height, fps=fps),
        layout=dict(kind='single' if layout == 'center' else 'blur_fill',
                    slots=[dict(name='main', rect=dict(x=0, y=0, w=width, h=height))]),
        video=[dict(slot='main', clips=[dict(id='main', media=media, src_in=start, src_out=start + duration,
                                             start=0, crop=crop, grade='restrained')])],
        audio=[dict(id='source', media=media, src_in=start, src_out=start + duration, start=0, role='source',
                    fade_in=0, fade_out=0)] if info.get('has_audio') else [],
        duration=duration, fade_in=min(.12, duration / 4), fade_out=min(.18, duration / 4))
    if caption:
        caption = Path(caption) if os.path.isabs(caption) else Path(os.path.abspath(caption))
        roots.append(str(caption.parent))
        margin = width // 12
        data['captions'] = dict(renderer='ass', ass_file=str(caption), cues=[],
                                safe_rect=dict(x=margin, y=height // 2, w=width - 2 * margin, h=height // 2 - height // 8))
    timeline = Timeline.model_validate(data, context={'allowed_roots': roots})
    return timeline, (width, height)


def render_command(source, output, start, duration, info, resolution, layout, caption=None):
    from ..editing.ffmpeg_graph import compile as compile_timeline
    timeline, dims = legacy_timeline(source, start, duration, info, resolution, layout, caption)
    # Inline graph: the caller runs argv itself and only guarantees cwd = caption folder.
    # strict_capabilities=False keeps the legacy behaviour of emitting ``ass`` whatever the build.
    plan = compile_timeline(timeline, None, output=output, inline=True, strict_capabilities=False)
    return plan.argv, dims


def validate_output(path, expected_duration, dimensions, audio):
    from ..editing.qa import validate_output as extended_validate
    return extended_validate(path, expected_duration, dimensions, audio)
