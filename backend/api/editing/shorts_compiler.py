"""Turn a selected highlight candidate into a validated ``Timeline`` (WP8).

``compile_candidate`` is the single producer of Shorts timelines. It combines the pure
building blocks (pacing, reframe, graphics, hook) according to ``presets.ResolvedOptions``:

1. dead-air keep-intervals (optional) -> one VideoClip/AudioSegment per kept interval,
   captions remapped through the same intervals so they stay in sync;
2. punch-ins (optional) -> step ``ZoomKey(mode='cut')`` per clip;
3. composition -> layout + crop: blur fill, centre, motion/edge reframing path
   (dynamic_center / subject_follow), letterbox, split screen (two crops of the source, or
   the source plus an owned secondary asset) or picture-in-picture;
4. grounded hook-title overlay (optional), rendered to PNG with ``graphics.render_card``;
5. captions: ASS when the local FFmpeg has libass, else PNG overlays
   (``CAPTION_RENDERER=auto|ass|png``).

All media paths in the timeline are MEDIA_ROOT-relative so the saved JSON can be
re-validated and recompiled later (``tasks.rerender_short``). Expensive analysis
(reframing decode, RMS windows) runs at most once per call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from django.conf import settings

from ..services import captions as caption_service
from ..services.media import StudioError
from . import graphics, hook, pacing, reframe
from .card_specs import HookTitle, TitleCard
from .ffmpeg_graph import ffmpeg_capabilities
from .presets import ResolvedOptions
from .timeline import Timeline

CAPTION_Z, HOOK_Z = 20, 30
HOOK_SECONDS = 3.0
MIN_INTERVAL = .12           # kept intervals shorter than this are merged back (no flash frames)
STATIC_LABELS = {
    'blur_fill': 'static blur-fill framing (whole frame visible); no reframing',
    'center': 'static centre crop; no reframing',
    'letterbox': 'static letterbox (whole frame visible); no reframing',
}


@dataclass
class CompiledShort:
    timeline: Timeline
    cues: list                      # output-time cues (what the captions and transcript editor show)
    summary: dict                   # short_data['timeline_summary']
    report: dict                    # extra quality_report sections
    srt: Path | None = None
    ass: Path | None = None
    warnings: list = field(default_factory=list)


# ------------------------------------------------------------------------------- helpers

def _even(value) -> int:
    return max(2, int(round(value / 2)) * 2)


def media_relative(path) -> str:
    root = Path(settings.MEDIA_ROOT).resolve()
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(root):
        raise StudioError('media_outside_root', 'Render inputs must live inside the media directory.')
    return str(resolved.relative_to(root))


def canvas_for(info, resolution):
    """Same output size/fps rule as the legacy ``render_command``."""
    width, height = (1080, 1920) if resolution == '1080' else (720, 1280)
    if info['height'] < 1080 and resolution == '1080':
        width, height = 720, 1280
    fps = 60 if info['fps'] >= 50 and resolution == '1080' else min(30, info['fps'])
    return width, height, fps


def caption_renderer() -> str:
    """'ass' when the local FFmpeg has libass (or CAPTION_RENDERER=ass), else 'png'."""
    choice = (os.getenv('CAPTION_RENDERER') or 'auto').strip().lower()
    libass = ffmpeg_capabilities().libass
    if choice == 'ass':
        if not libass:
            raise StudioError('libass_required', 'CAPTION_RENDERER=ass needs an FFmpeg build with libass. Install '
                              'ffmpeg-full on macOS or set FFMPEG_BIN, or use CAPTION_RENDERER=auto for PNG captions.')
        return 'ass'
    if choice == 'png':
        return 'png'
    return 'ass' if libass else 'png'


def _safe_rect(canvas):
    left, top, right, bottom = graphics.safe_rect(canvas)
    return dict(x=left, y=top, w=right - left, h=bottom - top)


def snap_candidate(candidate, cues, signals, duration, neighbours=()):
    """Snap a candidate onto sentence/scene boundaries (``pacing.snap_bounds``) without
    overlapping ``neighbours`` (the other selected candidates). Returns (start, end, reasons) or None."""
    bounds = pacing.snap_bounds(candidate['start'], candidate['end'], cues,
                                (signals or {}).get('scene_boundaries', ()), duration=duration)
    start, end = bounds.start, bounds.end
    if end - start < max(5.0, (candidate['end'] - candidate['start']) * .6) or end - start > 175:
        return None
    for other in neighbours:
        if start < other['end'] and end > other['start']:
            return None
    return start, end, {'start': bounds.start_reason, 'end': bounds.end_reason}


# ------------------------------------------------------------------------------- pacing

def _intervals(options, cues, signals, start, end, has_audio):
    """Kept clip-local intervals plus the pacing report."""
    length = end - start
    report = dict(enabled=options.remove_dead_air, removed_seconds=0.0, removed=[], protected=[], capped=False,
                  skipped=None, removal_ratio=0.0, content_category=options.content_category)
    if not options.remove_dead_air:
        return [(0.0, round(length, 3))], report
    silence = None
    if has_audio:
        silence = [(max(0.0, a - start), min(length, b - start))
                   for a, b in (signals or {}).get('silence', ()) if b > start and a < end]
    p = options.pacing
    result = pacing.keep_intervals(0.0, length, cues, silence, min_gap=p['min_gap'], pad=p['pad'],
                                   max_removal=p['max_removal'], dramatic_pause=p['dramatic_pause'])
    intervals = [list(i) for i in result.intervals]
    merged = []
    for a, b in intervals:
        if merged and (b - a < MIN_INTERVAL or merged[-1][1] - merged[-1][0] < MIN_INTERVAL):
            merged[-1][1] = b          # give the short piece back its gap rather than flash a few frames
        else:
            merged.append([a, b])
    intervals = [(round(a, 3), round(b, 3)) for a, b in merged]
    kept = sum(b - a for a, b in intervals)
    removed = [(round(intervals[i][1], 3), round(intervals[i + 1][0], 3)) for i in range(len(intervals) - 1)]
    report.update(removed_seconds=round(length - kept, 3), removed=removed, protected=result.protected,
                  capped=result.capped, skipped=result.skipped, removal_ratio=round((length - kept) / length, 4),
                  min_gap=p['min_gap'], max_removal=p['max_removal'])
    return intervals, report


def _zoom_for(keys, a, b, frame):
    """Step zoom keys (clip-local source time) -> ZoomKey dicts local to interval [a, b)."""
    if not keys:
        return []
    z0 = 1.0
    for t, z in keys:
        if t <= a + 1e-6:
            z0 = z
    out = [dict(t=0.0, zoom=round(z0, 3), mode='cut')] if z0 > 1.0 else []
    for t, z in keys:
        if a + 1e-6 < t < b - frame:
            local = round(t - a, 3)
            if out and local <= out[-1]['t']:
                continue
            if not out and z <= 1.0:
                continue
            if out and abs(out[-1]['zoom'] - z) < 1e-6:
                continue
            out.append(dict(t=local, zoom=round(min(1.6, max(1.0, z)), 3), mode='cut'))
    return out[:32]


# ------------------------------------------------------------------------------- crop paths

def _path_value(keys, t):
    """Crop centre at source time ``t`` from reframe keys (linear, jump at cut keys)."""
    prev = keys[0]
    if t <= prev.t:
        return prev.cx
    for k in keys[1:]:
        if t < k.t:
            if k.cut:
                return prev.cx
            span = k.t - prev.t
            return prev.cx + (k.cx - prev.cx) * ((t - prev.t) / span if span > 0 else 1)
        prev = k
    return prev.cx


def _path_crop(result, a_src, b_src, frame):
    """Reframe result -> Crop(mode='path') dict for the source range [a_src, b_src)."""
    safe = result.safe_area
    full = safe[0] < .01 and safe[1] < .01 and safe[2] > .99 and safe[3] > .99

    def norm(cx):
        if full:
            return round(min(1.0, max(0.0, cx)), 4)
        return round(min(1.0, max(0.0, (cx - safe[0]) / safe[2])), 4)

    times = [a_src]
    for k in result.keys:
        if a_src + frame < k.t < b_src - frame:
            if k.cut and k.t - frame > times[-1] + 1e-3:
                times.append(k.t - frame)
            times.append(k.t)
    if len(times) > 32:
        step = (len(times) - 1) / 31
        times = [times[round(i * step)] for i in range(32)]
    keys, last = [], -1.0
    for t in times:
        local = round(t - a_src, 3)
        if local <= last:
            continue
        keys.append(dict(t=local, cx=norm(_path_value(result.keys, t)), cy=.5))
        last = local
    crop = dict(mode='path', keys=keys)
    if not full:
        crop['safe_area'] = tuple(round(v, 4) for v in safe)
    return crop


# ------------------------------------------------------------------------------- captions

def build_caption_layer(cues, canvas, options: ResolvedOptions, folder: Path, tag, *, renderer=None,
                        legacy_style=None, commentary=None, duration=None):
    """Captions for output-time ``cues``.

    Returns dict(track, overlays, srt, ass, renderer, timing, warnings). ``track`` is the
    CaptionTrack dict (None when there is nothing to show). PNG captions refuse Devanagari
    without raqm (``text_shaping_unavailable``) rather than render broken conjuncts.
    """
    folder.mkdir(parents=True, exist_ok=True)
    W, H = canvas
    mode = options.subtitle_mode
    preset = options.caption_preset
    duration = duration if duration is not None else max((c['end'] for c in cues), default=0)
    out = dict(track=None, overlays=[], srt=None, ass=None, renderer=None, timing=None, warnings=[])
    if not cues and not commentary:
        return out
    renderer = renderer or caption_renderer()
    out['renderer'] = renderer
    safe = _safe_rect((W, H))
    clean = [dict(c, start=max(0.0, c['start']), end=min(duration, c['end'])) for c in cues
             if min(duration, c['end']) > max(0.0, c['start'])]
    track = dict(preset=preset, mode=mode, cues=[_cue_model(c) for c in clean], renderer=renderer, safe_rect=safe)
    if renderer == 'ass':
        style = legacy_style or preset
        srt, ass = caption_service.write_captions(clean, folder, tag, style, mode)
        if commentary:
            end = caption_service.stamp(min(5, duration))
            if not clean:
                srt.write_text(f'1\n{caption_service.stamp(0)} --> {end}\nANALYSIS: {commentary["on_screen_text"]}\n')
            overlay = commentary['on_screen_text'].replace('\\', '').replace('{', '').replace('}', '').replace('\n', ' ')
            with ass.open('a') as subtitle:
                subtitle.write(f'\nDialogue: 1,{caption_service.stamp(0, True)},{caption_service.stamp(min(5, duration), True)}'
                               f',Main,,0,0,0,,{{\\pos(540,330)\\fad(180,180)}}ANALYSIS: {overlay}\n')
        track['ass_file'] = media_relative(ass)
        out.update(track=track, srt=srt, ass=ass,
                   timing='word' if any(c.get('words') for c in clean) else 'approximate')
        return out
    groups = caption_service.caption_groups(clean, preset, mode)
    for g in groups:
        graphics.ensure_shaping(g['text'])
    srt = folder / f'{tag}.srt'
    caption_service.write_srt(groups, srt)
    if commentary and not groups:
        srt.write_text(f'1\n{caption_service.stamp(0)} --> {caption_service.stamp(min(5, duration))}\n'
                       f'ANALYSIS: {commentary["on_screen_text"]}\n')
    png_dir = folder / f'png_{tag}'
    items = [(g['start'], min(g['end'], duration), g['text']) for g in groups]
    words = [[(w[2], w[0], w[1]) for w in g['words']] if g['words'] else None for g in groups]
    rendered = graphics.render_caption_pngs(items, preset, mode, (W, H), png_dir,
                                            word_timings=words if any(words) else None)
    if len(rendered) > 240 and any(words):
        out['warnings'].append('Too many word-highlight states; captions rendered per phrase instead.')
        rendered = graphics.render_caption_pngs(items, preset, mode, (W, H), png_dir)
    overlays = []
    for i, item in enumerate(rendered[:240]):
        x, y, w, h = item['rect']
        if item['end'] - item['start'] < 1e-3:
            continue
        overlays.append(dict(id=f'cap{i}', kind='caption', png=media_relative(item['png']), start=round(item['start'], 3),
                             end=round(min(item['end'], duration), 3), rect=dict(x=x, y=y, w=w, h=h),
                             animation='none', z=CAPTION_Z))
    if commentary:
        title = ' '.join(commentary['on_screen_text'].split())[:72].strip() or 'Analysis'
        graphics.ensure_shaping(title)
        card = graphics.render_card(TitleCard(title=title, kicker='Analysis'), options.theme, (W, H), folder / 'cards')
        x, y, w, h = card.rect
        overlays.append(dict(id='commentary', kind='title_card', png=media_relative(card.path), start=0.0,
                             end=round(min(5.0, duration), 3), rect=dict(x=x, y=y, w=w, h=h), animation='fade',
                             z=HOOK_Z))
    out.update(track=track if clean else None, overlays=overlays, srt=srt,
               timing='word' if any(words) else 'approximate')
    return out


def _cue_model(cue):
    data = dict(start=round(float(cue['start']), 3), end=round(float(cue['end']), 3), text=cue.get('text', ''),
                translated_english=cue.get('translated_english') or '', translated_hindi=cue.get('translated_hindi') or '',
                detected_language=cue.get('detected_language') or 'und')
    words = caption_service._word_items(cue)
    if words:
        data['words'] = [dict(start=a, end=b, text=t) for a, b, t in words]
    if data['end'] <= data['start']:
        data['end'] = round(data['start'] + .05, 3)
    return data


# ------------------------------------------------------------------------------- main entry

def compile_candidate(source, info, candidate, cues, signals, options: ResolvedOptions, *, workdir, number,
                      source_license='unknown', secondary=None, commentary=None, legacy_style=None,
                      caption_folder=None, checkpoint=None) -> CompiledShort:
    """Build the Timeline for one Short.

    ``cues`` are clip-local (0 = candidate start). ``secondary`` (split screen only) is a dict
    with ``path, duration, width, height, has_audio, license_status, asset_id, kind`` for a
    MediaAsset the owner may use. ``checkpoint(message)`` is called between expensive stages so
    the caller can honour cancellation.
    """
    check = checkpoint or (lambda message: None)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    start, end = float(candidate['start']), float(candidate['end'])
    length = end - start
    W, H, fps = canvas_for(info, options.resolution)
    frame = 1 / fps
    has_audio = bool(info.get('has_audio'))
    warnings, report = [], {}
    source_rel = media_relative(source)
    src_media = dict(kind='source', path=source_rel, duration=max(float(info.get('duration') or 0), end),
                     has_audio=has_audio, width=info.get('width'), height=info.get('height'),
                     license_status=source_license or 'unknown')

    # 1. pacing
    intervals, pacing_report = _intervals(options, cues, signals, start, end, has_audio)
    out_cues = pacing.remap_items(cues, intervals) if len(intervals) > 1 or intervals[0][0] > 0 else \
        [dict(c) for c in cues]
    duration = round(sum(b - a for a, b in intervals), 3)
    report['pacing'] = pacing_report

    # 2. punch-ins (RMS analysed once for the clip)
    zoom_keys, points = [], []
    if options.punch_ins:
        check('Finding punch-in moments')
        peaks = []
        if has_audio:
            try:
                windows = pacing.audio_rms_windows(str(Path(source).resolve()), start, end)
                peaks = [(round(t - start, 3), db) for t, db in pacing.rms_peaks(windows)]
            except StudioError as exc:
                warnings.append(f'Audio peak analysis unavailable ({exc.code}); punch-ins use words only.')
        pi = options.punch_in
        points = pacing.punch_points(0.0, length, cues, peaks, min_spacing=pi.spacing, zoom=pi.zoom,
                                     hold_max=pi.hold_max)
        zoom_keys = pacing.zoom_keys(points, 0.0)
    report['punch_ins'] = dict(enabled=options.punch_ins, count=len(points),
                               points=[{k: p[k] for k in ('t', 'end', 'zoom', 'reason', 'detail')} for p in points])

    # 3. composition
    composition = options.composition
    grade = options.grade
    crop_report = dict(composition=composition)
    slots, tracks = [], []

    def clips_for(media, crop_fn, zoom=False, prefix='c'):
        out, cursor = [], 0.0
        for i, (a, b) in enumerate(intervals):
            clip = dict(id=f'{prefix}{i}', media=media, src_in=round(start + a, 3), src_out=round(start + b, 3),
                        start=round(cursor, 3), crop=crop_fn(start + a, start + b), grade=grade)
            if zoom:
                keys = _zoom_for(zoom_keys, a, b, frame)
                if keys:
                    clip['zoom'] = keys
            out.append(clip)
            cursor += b - a
        return out

    label = STATIC_LABELS.get(composition)
    if composition in ('dynamic_center', 'subject_follow'):
        check('Reframing the shot')
        result = None
        try:
            result = reframe.reframe(str(Path(source).resolve()), start, end, workdir, mode=composition,
                                     scene_boundaries=(signals or {}).get('scene_boundaries', ()),
                                     src_w=info.get('width'), src_h=info.get('height'))
        except (StudioError, ValueError, OSError) as exc:
            warnings.append(f'Reframing failed ({getattr(exc, "code", type(exc).__name__)}); used a centre crop.')
        if result:
            slots = [dict(name='main', rect=dict(x=0, y=0, w=W, h=H))]
            tracks = [dict(slot='main', clips=clips_for(src_media, lambda a, b: _path_crop(result, a, b, frame), True))]
            crop_report.update(mode='path', reframe=dict(confident_ratio=result.confident_ratio, keys=len(result.keys),
                               segments=result.segments, notes=result.notes, timings=result.timings,
                               limitations=list(reframe.LIMITATIONS)))
            label = reframe.LABEL
            layout_kind = 'single'
        else:
            label = STATIC_LABELS['center'] + ' (reframing unavailable)'
            crop_report['fallback'] = 'center'
    if not tracks and composition in ('dynamic_center', 'subject_follow', 'center', 'blur_fill', 'letterbox'):
        mode = {'blur_fill': 'fit_blur', 'letterbox': 'letterbox'}.get(composition, 'center')
        layout_kind = {'blur_fill': 'blur_fill', 'letterbox': 'letterbox'}.get(composition, 'single')
        slots = [dict(name='main', rect=dict(x=0, y=0, w=W, h=H))]
        tracks = [dict(slot='main', clips=clips_for(src_media, lambda a, b: {'mode': mode}, True))]
        crop_report['mode'] = mode
    elif composition == 'split_screen':
        layout_kind = 'split_v'
        src_w, src_h = info.get('width') or W, info.get('height') or H
        if secondary:
            top_h = _even(H * .5)
            slots = [dict(name='top', rect=dict(x=0, y=0, w=W, h=top_h)),
                     dict(name='bottom', rect=dict(x=0, y=top_h, w=W, h=H - top_h))]
            sec_media = dict(kind='asset', path=secondary['path'], asset_id=secondary['asset_id'],
                             duration=float(secondary['duration']), has_audio=bool(secondary.get('has_audio')),
                             width=secondary.get('width'), height=secondary.get('height'),
                             license_status=secondary.get('license_status') or 'unknown')
            sec_clips, cursor, i = [], 0.0, 0
            still = secondary.get('kind') == 'image'
            while duration - cursor > frame / 2 and i < 60:
                take = (duration - cursor) if still else min(float(secondary['duration']), duration - cursor)
                if take <= frame / 2:
                    break
                sec_clips.append(dict(id=f's{i}', media=sec_media, src_in=0.0, src_out=round(take, 3),
                                      start=round(cursor, 3), crop={'mode': 'center'}, grade=grade, still=still))
                cursor += round(take, 3)
                i += 1
            if duration - cursor > frame:
                raise StudioError('secondary_too_short', 'The secondary clip is too short to fill the split screen.')
            # Rounded tiles must end exactly at the timeline end.
            last = sec_clips[-1]
            last['src_out'] = round(last['src_out'] + (duration - cursor), 3)
            tracks = [dict(slot='top', clips=clips_for(src_media, lambda a, b: {'mode': 'center'}, True, 'm')),
                      dict(slot='bottom', clips=sec_clips)]
            label = ('split screen: source (centre crop) above an owned secondary clip; '
                     'fixed framing, no face detection')
            crop_report.update(mode='split_v', secondary=dict(asset_id=secondary['asset_id'],
                               license_status=sec_media['license_status'], tiles=len(sec_clips)))
        else:
            top_h = _even(H * .35)
            regions = reframe.split_screen_regions(src_w, src_h, options.facecam_corner, face_panel=top_h / H,
                                                   out_w=W, out_h=H)
            slots = [dict(name='top', rect=dict(x=0, y=0, w=W, h=top_h)),
                     dict(name='bottom', rect=dict(x=0, y=top_h, w=W, h=H - top_h))]
            tracks = [dict(slot='top', clips=clips_for(src_media, lambda a, b: {'mode': 'fixed', 'region': regions['facecam']},
                                                       False, 'f')),
                      dict(slot='bottom', clips=clips_for(src_media, lambda a, b: {'mode': 'fixed', 'region': regions['gameplay']},
                                                          True, 'g'))]
            label = regions['label']
            crop_report.update(mode='split_v', regions=dict(facecam=regions['facecam'], gameplay=regions['gameplay'],
                                                            corner=regions['corner']))
    elif composition == 'pip':
        layout_kind = 'pip'
        src_w, src_h = info.get('width') or W, info.get('height') or H
        left, top, right, bottom = graphics.safe_rect((W, H))
        pip_w = _even(W * .4)
        pip_h = _even(pip_w * 9 / 16)
        corner = options.facecam_corner
        x = left if 'left' in corner else right - pip_w
        y = top + round(.14 * H) if corner.startswith('top') else bottom - pip_h - round(.2 * H)
        # face_panel * out_h == pip_h, so the facecam region takes the pip box aspect.
        regions = reframe.split_screen_regions(src_w, src_h, corner, face_panel=.5, out_w=pip_w, out_h=pip_h * 2)
        slots = [dict(name='main', rect=dict(x=0, y=0, w=W, h=H)),
                 dict(name='pip', rect=dict(x=int(x), y=int(y), w=pip_w, h=pip_h), z=1)]
        tracks = [dict(slot='main', clips=clips_for(src_media, lambda a, b: {'mode': 'center'}, True, 'm')),
                  dict(slot='pip', clips=clips_for(src_media, lambda a, b: {'mode': 'fixed', 'region': regions['facecam']},
                                                   False, 'p'))]
        label = 'picture-in-picture: fixed facecam box from a preset corner; no face detection'
        crop_report.update(mode='pip', regions=dict(facecam=regions['facecam'], corner=corner))
    if not tracks:
        raise StudioError('invalid_composition', f'Unsupported composition {composition!r}.')
    crop_report['label'] = label

    # 4. audio (source only; a secondary clip never contributes sound)
    audio = []
    if has_audio:
        cursor = 0.0
        multi = len(intervals) > 1
        for i, (a, b) in enumerate(intervals):
            fade = .015 if multi else 0
            audio.append(dict(id=f'a{i}', media=src_media, src_in=round(start + a, 3), src_out=round(start + b, 3),
                              start=round(cursor, 3), role='source', fade_in=fade, fade_out=fade))
            cursor += b - a

    # 5. hook overlay (extractive, grounded in the clip transcript)
    overlays, hook_info = [], None
    hook_report = dict(enabled=options.hook_overlay, source=None, reason=None)
    if options.hook_overlay and commentary:
        hook_report['reason'] = 'original analysis card occupies the title area'
    elif options.hook_overlay:
        transcript = ' '.join(c.get('text', '') for c in cues).strip()
        picked = hook.hook_overlay(transcript) if transcript else None
        sentences = hook.sentences(transcript) if transcript else []
        if picked and sentences:
            # Prefer the whole opening sentence when the extractive pick is a fragment of it and it fits.
            full = sentences[0] if sentences[0].endswith('?') else sentences[0].rstrip('.,;:!…')
            if (picked['text'] != full and picked['text'] in full and len(full) <= hook.MAX_OVERLAY_CHARS
                    and len(full.split()) <= hook.MAX_OVERLAY_WORDS and hook.is_grounded(full, transcript)):
                picked = dict(picked, text=full)
        if not picked:
            hook_report['reason'] = 'no grounded hook phrase in the clip transcript' if transcript else 'no transcript'
        elif not hook.is_grounded(picked['text'], transcript):
            hook_report['reason'] = 'candidate hook text was not grounded in the transcript'
        else:
            try:
                card = graphics.render_card(HookTitle(text=picked['text']), options.theme, (W, H), workdir / 'cards')
                x, y, w, h = card.rect
                overlays.append(dict(id='hook', kind='hook_title', png=media_relative(card.path), start=0.0,
                                     end=round(min(HOOK_SECONDS, duration - frame), 3), rect=dict(x=x, y=y, w=w, h=h),
                                     animation=options.hook_animation, z=HOOK_Z,
                                     spec=dict(kind='hook_title', text=picked['text'], theme=options.theme)))
                hook_info = dict(text=picked['text'], source=picked['source'])
                hook_report.update(source=picked['source'], text=picked['text'])
            except StudioError as exc:
                hook_report['reason'] = exc.message
                warnings.append(f'Hook title skipped: {exc.message}')

    # 6. captions
    check('Building captions')
    layer = build_caption_layer(out_cues, (W, H), options, Path(caption_folder or workdir), number,
                                legacy_style=legacy_style, commentary=commentary, duration=duration)
    warnings += layer['warnings']
    overlays += layer['overlays']

    data = dict(canvas=dict(width=W, height=H, fps=fps), layout=dict(kind=layout_kind, slots=slots), video=tracks,
                overlays=overlays, captions=layer['track'], audio=audio, duration=duration,
                fade_in=min(options.fade_in, duration / 4), fade_out=min(options.fade_out, duration / 4))
    timeline = Timeline.model_validate(data)
    summary = dict(preset=options.preset, composition=options.composition if not crop_report.get('fallback')
                   else crop_report['fallback'], caption_renderer=layer['renderer'],
                   removed_seconds=pacing_report['removed_seconds'], punch_ins=len(points), hook_overlay=hook_info,
                   reframe_label=label, caption_preset=options.caption_preset)
    report.update(crop=crop_report, hook_overlay=hook_report, caption_renderer=layer['renderer'],
                  caption_preset=options.caption_preset, caption_word_timing=layer['timing'] == 'word')
    return CompiledShort(timeline, out_cues, summary, report, layer['srt'], layer['ass'], warnings)


def recaption(timeline_data, cues, options: ResolvedOptions, folder, tag, legacy_style=None):
    """Replace the caption layer of a saved timeline with captions for edited output-time ``cues``.

    Returns (Timeline, layer dict). Video, audio, crops, zooms and the hook overlay are kept.
    """
    data = dict(timeline_data)
    data.pop('provenance', None)
    duration = float(data['duration'])
    W, H = data['canvas']['width'], data['canvas']['height']
    previous = data.get('captions') or {}
    if previous.get('preset'):
        options = _with(options, caption_preset=previous['preset'], subtitle_mode=previous.get('mode') or options.subtitle_mode)
    layer = build_caption_layer(cues, (W, H), options, Path(folder), tag, legacy_style=legacy_style, duration=duration)
    data['overlays'] = [o for o in data.get('overlays', []) if o.get('kind') != 'caption'] + \
        [o for o in layer['overlays'] if o['kind'] == 'caption']
    data['captions'] = layer['track']
    return Timeline.model_validate(data), layer


def _with(options, **changes):
    return replace(options, **changes)
