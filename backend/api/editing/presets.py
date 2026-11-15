"""Editing presets for generated Shorts (WP8).

A preset is a bundle of *defaults*: composition, caption preset, colour grade, pacing
aggressiveness, punch-in frequency and the card theme for the hook title. Explicit job
options always win over the preset. The three effect switches (``hook_overlay``,
``remove_dead_air``, ``punch_ins``) are explicit opt-ins that default to off; a preset
only decides *how strongly* an enabled effect is applied (``suggested`` lists the switches a
UI may pre-tick for that preset, nothing turns on by itself).

``preset='none'`` with every switch off reproduces the legacy render exactly: legacy
``layout`` ('preserve' -> blur fill, 'center' -> centre crop), restrained grade, the legacy
``caption_style`` mapped through ``graphics.LEGACY_STYLE_MAP`` and the same programme fades.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .graphics import LEGACY_STYLE_MAP

PRESET_NAMES = ('none', 'gaming_hype', 'cinematic_gaming', 'viral_commentary', 'minimal_premium',
                'split_screen_gaming')
COMPOSITIONS = ('auto', 'blur_fill', 'center', 'dynamic_center', 'subject_follow', 'letterbox', 'split_screen', 'pip')
CAPTION_PRESETS = ('auto', 'bold_gaming', 'cinematic', 'dynamic_creator', 'clean_commentary', 'bilingual')
CATEGORIES = ('gaming', 'commentary', 'tutorial', 'podcast', 'vlog', 'other')
CORNERS = ('top-left', 'top-right', 'bottom-left', 'bottom-right')


@dataclass(frozen=True)
class Pacing:
    """Dead-air removal strength. ``min_gap`` scales the category's base pause threshold."""
    gap_scale: float = 1.0
    max_removal: float = .2
    pad: float = .12


@dataclass(frozen=True)
class PunchIn:
    spacing: float = 7.0      # minimum seconds between punch-ins
    zoom: float = 1.1
    hold_max: float = 2.5


@dataclass(frozen=True)
class EditPreset:
    name: str
    label: str
    description: str
    composition: str
    caption_preset: str
    grade: str
    theme: str | None
    pacing: Pacing
    punch_in: PunchIn
    hook_animation: str = 'fade'
    fade_in: float = .12
    fade_out: float = .18
    suggested: dict = field(default_factory=dict)

    def as_dict(self):
        return asdict(self)


PRESETS: dict[str, EditPreset] = {p.name: p for p in (
    EditPreset('none', 'No preset', 'Legacy render: blur-fill or centre crop, restrained grade, legacy captions.',
               composition='legacy', caption_preset='legacy', grade='restrained', theme='minimal_premium',
               pacing=Pacing(1.0, .2), punch_in=PunchIn(8.0, 1.08)),
    EditPreset('gaming_hype', 'Gaming hype',
               'Motion-following 9:16 crop, punchy grade, bold yellow captions, tight pacing and frequent jump zooms.',
               composition='dynamic_center', caption_preset='bold_gaming', grade='punchy', theme='gaming_hype',
               pacing=Pacing(.8, .25, .1), punch_in=PunchIn(5.0, 1.12, 2.0), hook_animation='pop',
               suggested=dict(hook_overlay=True, remove_dead_air=True, punch_ins=True)),
    EditPreset('cinematic_gaming', 'Cinematic gaming',
               'Full frame letterboxed with headroom, filmic grade, understated serif captions, gentle pacing.',
               composition='letterbox', caption_preset='cinematic', grade='filmic', theme='cinematic_gaming',
               pacing=Pacing(1.5, .12, .16), punch_in=PunchIn(10.0, 1.06, 3.0), hook_animation='fade',
               fade_in=.35, fade_out=.5, suggested=dict(hook_overlay=True, remove_dead_air=False, punch_ins=False)),
    EditPreset('viral_commentary', 'Viral commentary',
               'Crop follows the speaker area, creator-style captions, aggressive dead-air removal and punch-ins.',
               composition='subject_follow', caption_preset='dynamic_creator', grade='punchy', theme='viral_commentary',
               pacing=Pacing(.7, .25, .1), punch_in=PunchIn(4.0, 1.15, 2.0), hook_animation='slide_up',
               suggested=dict(hook_overlay=True, remove_dead_air=True, punch_ins=True)),
    EditPreset('minimal_premium', 'Minimal premium',
               'Blur-fill framing, restrained grade, clean captions on a pill; light pacing, no gimmicks.',
               composition='blur_fill', caption_preset='clean_commentary', grade='restrained', theme='minimal_premium',
               pacing=Pacing(1.3, .15, .14), punch_in=PunchIn(9.0, 1.06, 3.0), hook_animation='fade',
               suggested=dict(hook_overlay=False, remove_dead_air=True, punch_ins=False)),
    EditPreset('split_screen_gaming', 'Split-screen gaming',
               'Facecam panel on top and gameplay below (or a second owned clip), bold captions.',
               composition='split_screen', caption_preset='bold_gaming', grade='punchy', theme='split_screen_gaming',
               pacing=Pacing(1.0, .2, .12), punch_in=PunchIn(7.0, 1.08, 2.5), hook_animation='pop',
               suggested=dict(hook_overlay=True, remove_dead_air=False, punch_ins=False)),
)}

# Base pause threshold (seconds) and protected dramatic pause per content category. Conversation
# (podcast) keeps more breathing room; gaming and commentary are cut tighter.
CATEGORY_PACING = {
    'gaming': dict(min_gap=.55, dramatic_pause=.7),
    'commentary': dict(min_gap=.6, dramatic_pause=.8),
    'tutorial': dict(min_gap=.7, dramatic_pause=.8),
    'podcast': dict(min_gap=.9, dramatic_pause=1.0),
    'vlog': dict(min_gap=.65, dramatic_pause=.8),
    'other': dict(min_gap=.6, dramatic_pause=.8),
}


@dataclass(frozen=True)
class ResolvedOptions:
    """Effective editing choices for one job, after preset defaults and explicit overrides."""
    preset: str
    composition: str
    caption_preset: str
    subtitle_mode: str
    grade: str
    theme: str
    hook_overlay: bool
    remove_dead_air: bool
    punch_ins: bool
    content_category: str
    facecam_corner: str
    secondary_asset_id: str | None
    pacing: dict
    punch_in: PunchIn
    hook_animation: str
    fade_in: float
    fade_out: float
    resolution: str
    legacy: bool              # preset none + no switches: byte-for-byte legacy graph

    def as_dict(self):
        data = asdict(self)
        return data


def _choice(value, allowed, default):
    return value if value in allowed else default


def resolve(settings: dict) -> ResolvedOptions:
    """Combine a job's stored ``settings`` (JobOptions.model_dump) into effective choices.

    Unknown or missing values fall back to defaults so jobs created before WP8 resolve to the
    legacy behaviour.
    """
    settings = settings or {}
    name = _choice(settings.get('preset'), PRESET_NAMES, 'none')
    preset = PRESETS[name]
    composition = _choice(settings.get('composition'), COMPOSITIONS, 'auto')
    if composition == 'auto':
        composition = preset.composition
    if composition == 'legacy':
        composition = 'center' if settings.get('layout') == 'center' else 'blur_fill'
    caption = _choice(settings.get('caption_preset'), CAPTION_PRESETS, 'auto')
    mode = settings.get('subtitle_mode') or 'original'
    if caption == 'auto':
        if mode == 'bilingual':
            caption = 'bilingual'
        elif preset.caption_preset == 'legacy':
            caption = LEGACY_STYLE_MAP.get(settings.get('caption_style') or 'minimal', 'clean_commentary')
        else:
            caption = preset.caption_preset
    category = _choice(settings.get('content_category'), CATEGORIES, 'gaming')
    base = CATEGORY_PACING[category]
    pacing = dict(min_gap=round(base['min_gap'] * preset.pacing.gap_scale, 3), max_removal=preset.pacing.max_removal,
                  pad=preset.pacing.pad, dramatic_pause=base['dramatic_pause'])
    hook, dead_air, punch = (bool(settings.get(k)) for k in ('hook_overlay', 'remove_dead_air', 'punch_ins'))
    secondary = settings.get('secondary_asset_id') or None
    return ResolvedOptions(
        preset=name, composition=composition, caption_preset=caption, subtitle_mode=mode, grade=preset.grade,
        theme=preset.theme or 'minimal_premium', hook_overlay=hook, remove_dead_air=dead_air, punch_ins=punch,
        content_category=category, facecam_corner=_choice(settings.get('facecam_corner'), CORNERS, 'top-right'),
        secondary_asset_id=str(secondary) if secondary else None, pacing=pacing, punch_in=preset.punch_in,
        hook_animation=preset.hook_animation, fade_in=preset.fade_in, fade_out=preset.fade_out,
        resolution=settings.get('resolution') or '720',
        legacy=(name == 'none' and not (hook or dead_air or punch)
                and settings.get('composition', 'auto') == 'auto' and settings.get('caption_preset', 'auto') == 'auto'))


def catalogue():
    """Plain-data description of the presets (for API/UI listings)."""
    return [dict(p.as_dict(), suggested=dict(p.suggested)) for p in PRESETS.values()]
