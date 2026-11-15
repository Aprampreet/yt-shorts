"""Storyboard -> Timeline for Story Studio videos.

The narration asset is the master clock: the timeline lasts exactly as long as the narration and the scenes tile it.
Scene visuals are the owner's assets (fit/crop for video, slow eased zoom for stills) or a dark colour background
under a Pillow-rendered card. Captions come from the narration word timings as PNG overlays (or ASS when only libass
can shape the text). Devanagari is never rendered without a shaping engine: with neither raqm nor libass the video
fails with ``text_shaping_unavailable``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..models import LICENSE_REVIEW_REQUIRED
from ..services import assets as asset_svc
from ..services.media import StudioError
from ..story.alignment import approximate_words
from . import card_specs, graphics
from .ffmpeg_graph import ffmpeg_capabilities
from .timeline import Timeline

BACKGROUND = {'gaming_hype': '#101114', 'cinematic_gaming': '#0c0b0a', 'viral_commentary': '#120d10',
              'minimal_premium': '#141416', 'split_screen_gaming': '#0b1210'}
CARD_MODELS = {'fact_card': card_specs.FactCard, 'comparison_card': card_specs.ComparisonCard,
               'callout': card_specs.Callout, 'numbered_list': card_specs.NumberedList,
               'title_card': card_specs.TitleCard}
ANIMATION = {'title_card': 'slide_up', 'fact_card': 'slide_up', 'callout': 'pop', 'numbered_list': 'fade',
             'comparison_card': 'fade'}
TRANSITION_SECONDS = 0.3
GROUP_WORDS = 4
STRONG = ('.', '!', '?', '।', '॥', '…')
SHAPING_MESSAGE = ('Hindi (Devanagari) text cannot be rendered correctly on this machine: Pillow has no raqm layout '
                   'engine and this FFmpeg build has no libass. Install fribidi (brew install fribidi) or an FFmpeg build '
                   'with libass (ffmpeg-full), then render again. Nothing was rendered with broken conjuncts.')


@dataclass
class StoryCompile:
    timeline: Timeline
    groups: list                      # caption groups [(start, end, text)]
    provenance: list                  # per-asset licence report
    warnings: list = field(default_factory=list)
    caption_renderer: str = 'png'
    word_timing: str = 'approximate'
    cards: int = 0


def _media(asset, duration=None, has_audio=False):
    return {'kind': 'asset', 'path': asset.file, 'asset_id': str(asset.pk), 'duration': float(duration or asset.duration),
            'has_audio': has_audio, 'width': asset.width or None, 'height': asset.height or None,
            'license_status': asset.license_status}


def caption_groups(words, duration, max_words=GROUP_WORDS):
    groups, current = [], []
    for w in words:
        text = (w.get('text') or '').strip()
        if not text:
            continue
        if current and (len(current) >= max_words or float(w['start']) - float(current[-1]['end']) > 0.45):
            groups.append(current)
            current = []
        current.append(w)
        if text.endswith(STRONG):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    out = []
    for i, g in enumerate(groups):
        start = max(0.0, float(g[0]['start']))
        end = min(duration, float(g[-1]['end']))
        if i + 1 < len(groups):
            nxt = float(groups[i + 1][0]['start'])
            if 0 < nxt - end < 0.3:
                end = nxt
        if end - start < 0.12:
            end = min(duration, start + 0.12)
        if end > start:
            out.append((round(start, 3), round(end, 3), ' '.join(w['text'] for w in g)))
    clean = []
    for s, e, t in out:          # no overlaps
        if clean and s < clean[-1][1]:
            s = clean[-1][1]
        if e - s >= 0.05:
            clean.append((s, e, t))
    return clean


def _tile(scenes, duration):
    """[(scene, start, end)] contiguous over [0, duration]."""
    rows, cursor = [], 0.0
    ordered = sorted(scenes, key=lambda s: s.order)
    for i, scene in enumerate(ordered):
        end = duration if i == len(ordered) - 1 else min(duration, max(cursor, float(scene.end)))
        if end - cursor < 0.2 and i != len(ordered) - 1:
            continue
        rows.append((scene, round(cursor, 3), round(end, 3)))
        cursor = end
    if not rows:
        raise StudioError('storyboard_required', 'Build the storyboard before rendering.')
    if rows[-1][2] - rows[-1][1] < 0.1 and len(rows) > 1:
        scene, start, _ = rows.pop()
        prev = rows.pop()
        rows.append((prev[0], prev[1], round(duration, 3)))
    return rows


def _card_model(scene):
    card = (scene.graphic_spec or {}).get('card')
    if not card:
        return None
    model = CARD_MODELS.get(card.get('kind'))
    if model is None:
        raise StudioError('invalid_scene', f'Scene {scene.order} has an unsupported card.')
    return model.model_validate(card)


def compile_script(script, scenes, workdir, *, canvas=(720, 1280), fps=30, theme='gaming_hype',
                   caption_preset='clean_commentary', music_asset=None, capabilities=None, project_source=None):
    workdir = Path(workdir)
    narration = script.narration_asset
    if narration is None or narration.deleted_at is not None:
        raise StudioError('narration_required', 'This script has no narration.')
    asset_svc.asset_path(narration)
    if not narration.duration:
        narration.duration = asset_svc.probe_av(asset_svc.asset_path(narration), 'audio')['duration']
    D = round(float(narration.duration), 3)
    W, H = canvas
    caps = capabilities or ffmpeg_capabilities()
    warnings = []
    words = list((narration.metadata or {}).get('words') or [])
    timing = (narration.metadata or {}).get('timing') or 'none'
    if not words:
        words, timing = approximate_words(script.full_text, D), 'approximate'
    groups = caption_groups(words, D)
    cards = {scene.pk: _card_model(scene) for scene in scenes}

    texts = [t for _, _, t in groups] + [s for m in cards.values() if m for s in graphics._strings(m.model_dump())]
    devanagari = any(graphics.has_devanagari(t) for t in texts)
    raqm = graphics.raqm_available()
    if devanagari and not raqm and not caps.libass:
        raise StudioError('text_shaping_unavailable', SHAPING_MESSAGE)
    renderer = 'ass' if devanagari and not raqm else 'png'

    rows = _tile(scenes, D)
    background = BACKGROUND.get(theme, '#101114')
    clips, overlays, provenance = [], [], {}

    def record(asset, used_as, order):
        entry = provenance.setdefault(str(asset.pk), {
            'asset_id': str(asset.pk), 'title': asset.title, 'kind': asset.kind, 'origin': asset.origin,
            'license_status': asset.license_status, 'attribution_text': asset.attribution_text,
            'license_reference': asset.license_reference, 'provider': asset.provider, 'used_as': used_as,
            'scenes': [], 'third_party_source_footage': asset.origin == 'source_ingest'})
        if order is not None:
            entry['scenes'].append(order)

    record(narration, 'narration', None)
    for idx, (scene, start, end) in enumerate(rows):
        length = round(end - start, 3)
        transition = {'kind': scene.transition if idx else 'cut',
                      'duration': min(TRANSITION_SECONDS, length / 3) if scene.transition != 'cut' and idx else 0}
        asset = scene.asset
        cid = f's{scene.order}'
        if asset is not None:
            if asset.deleted_at is not None or asset.owner != script.project.owner:
                raise StudioError('asset_unavailable', f'Scene {scene.order} uses an asset that is no longer in your library.')
            if asset.license_status == 'restricted':
                raise StudioError('restricted_asset', f'Scene {scene.order} uses the restricted asset "{asset.title}". '
                                  'Replace it before rendering.')
            asset_svc.asset_path(asset)
        if asset is not None and scene.visual_type == 'asset_image':
            clips.append({'id': cid, 'media': _media(asset, duration=length), 'src_in': 0, 'src_out': length,
                          'start': start, 'still': True, 'crop': {'mode': 'center'}, 'grade': 'restrained',
                          'zoom': [{'t': 0, 'zoom': 1.0, 'mode': 'ease'}, {'t': length, 'zoom': 1.08, 'mode': 'ease'}],
                          'transition_in': transition})
            record(asset, 'scene_image', scene.order)
        elif asset is not None and scene.visual_type in ('asset_video', 'source_clip'):
            a_in = float(scene.asset_in or 0)
            limit = float(asset.duration or 0)
            if scene.asset_out is not None:
                limit = min(limit, float(scene.asset_out))
            seg = round(min(length, limit - a_in), 3)
            if seg < 0.2:
                raise StudioError('invalid_scene', f'Scene {scene.order}: the asset range is too short.')
            clips.append({'id': cid, 'media': _media(asset), 'src_in': round(a_in, 3), 'src_out': round(a_in + seg, 3),
                          'start': start, 'crop': {'mode': 'center'}, 'grade': 'restrained', 'transition_in': transition})
            if length - seg > 0.01:
                clips.append({'id': cid + 'f', 'media': {'kind': 'color', 'duration': length - seg, 'color': background},
                              'src_in': 0, 'src_out': round(length - seg, 3), 'start': round(start + seg, 3)})
                warnings.append(f'Scene {scene.order}: the clip is shorter than the scene; the rest shows a plain background.')
            record(asset, 'source_clip' if scene.visual_type == 'source_clip' else 'scene_video', scene.order)
        else:
            clips.append({'id': cid, 'media': {'kind': 'color', 'duration': length, 'color': background},
                          'src_in': 0, 'src_out': length, 'start': start, 'transition_in': transition})
        model = cards.get(scene.pk)
        if model is None:
            continue
        if any(graphics.has_devanagari(s) for s in graphics._strings(model.model_dump())) and not raqm:
            warnings.append(f'Scene {scene.order}: Devanagari card skipped (no raqm shaping); captions carry the text.')
            continue
        render = graphics.render_card(model, theme, (W, H), workdir/'cards')
        paths = render.paths
        span = (end - start) / len(paths)
        for k, path in enumerate(paths):
            a = round(start + k * span, 3)
            b = round(end if k == len(paths) - 1 else start + (k + 1) * span, 3)
            if b - a < 0.15:
                continue
            x, y, w, h = render.rect
            overlays.append({'id': f'card{scene.order}_{k}', 'kind': model.kind, 'png': str(path), 'spec': model.model_dump(),
                             'start': a, 'end': b, 'rect': {'x': x, 'y': y, 'w': w, 'h': h},
                             'animation': ANIMATION.get(model.kind, 'fade') if k == 0 else 'none', 'z': 10,
                             'license_status': 'generated'})
    n_cards = len(overlays)
    left, top, right, bottom = graphics.safe_rect((W, H))
    safe = {'x': left, 'y': top, 'w': right - left, 'h': bottom - top}
    cues = [{'start': s, 'end': e, 'text': t} for s, e, t in groups]
    if renderer == 'png' and groups:
        rendered = graphics.render_caption_pngs(groups, caption_preset, 'original', (W, H), workdir/'captions')
        for i, item in enumerate(rendered):
            x, y, w, h = item['rect']
            overlays.append({'id': f'cap{i}', 'kind': 'caption', 'png': item['png'], 'start': item['start'],
                             'end': min(D, item['end']), 'rect': {'x': x, 'y': y, 'w': w, 'h': h},
                             'animation': 'none', 'z': 20, 'license_status': 'generated'})
    elif renderer == 'ass':
        warnings.append('Captions burned with libass because Pillow raqm shaping is unavailable.')
    audio = [{'id': 'narration', 'media': _media(narration, has_audio=True), 'src_in': 0,
              'src_out': min(D, float(narration.duration)), 'start': 0, 'role': 'narration', 'fade_in': 0.015,
              'fade_out': 0.05}]
    if music_asset is not None:
        if music_asset.license_status == 'restricted':
            raise StudioError('restricted_asset', f'The background music "{music_asset.title}" is restricted.')
        if music_asset.deleted_at is not None or music_asset.owner != script.project.owner:
            raise StudioError('asset_unavailable', 'The background music asset is no longer in your library.')
        asset_svc.asset_path(music_asset)
        length = round(min(D, float(music_asset.duration or 0)), 3)
        if length > 1:
            audio.append({'id': 'music', 'media': _media(music_asset, has_audio=True), 'src_in': 0, 'src_out': length,
                          'start': 0, 'role': 'music', 'gain_db': -14, 'fade_in': 0.5, 'fade_out': min(1.0, length / 4),
                          'duck_under': ['narration']})
            record(music_asset, 'music', None)
    data = {'canvas': {'width': W, 'height': H, 'fps': fps},
            'layout': {'kind': 'single', 'slots': [{'name': 'main', 'rect': {'x': 0, 'y': 0, 'w': W, 'h': H}}]},
            'video': [{'slot': 'main', 'clips': clips}], 'overlays': overlays, 'audio': audio, 'duration': D,
            'captions': {'preset': caption_preset, 'mode': 'original', 'cues': cues, 'renderer': renderer,
                         'safe_rect': safe} if cues else None}
    try:
        timeline = Timeline.model_validate(data)
    except ValueError as exc:
        raise StudioError('timeline_invalid', f'Could not build a valid timeline: {str(exc)[:300]}') from None
    return StoryCompile(timeline, groups, list(provenance.values()), warnings, renderer, timing, n_cards)


def rights_of(provenance, *, synthetic_voice=False):
    blocked = [p for p in provenance if p['license_status'] == 'restricted']
    review = [p for p in provenance if p['license_status'] in LICENSE_REVIEW_REQUIRED and p['license_status'] != 'restricted']
    third_party = [p for p in provenance if p.get('third_party_source_footage')]
    status = 'manual_review_required' if review or blocked or third_party else 'rights_documented'
    return {'status': status, 'blocked': [p['asset_id'] for p in blocked],
            'review_required': [{'asset_id': p['asset_id'], 'title': p['title'], 'license_status': p['license_status']}
                                for p in review],
            'third_party_source_footage': [p['asset_id'] for p in third_party],
            'attribution': [p['attribution_text'] for p in provenance if p['license_status'] == 'attribution_required'],
            'synthetic_voice': synthetic_voice}
