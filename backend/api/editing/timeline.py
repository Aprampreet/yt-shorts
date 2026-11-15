"""Edit decision list shared by the Shorts pipeline and Story Studio.

A ``Timeline`` is pure data: it says which media ranges appear where, how they are
cropped, graded and mixed. ``ffmpeg_graph.compile`` turns it into one FFmpeg argv.

Media paths are MEDIA_ROOT-relative. Absolute paths are accepted only when they
resolve inside MEDIA_ROOT, or inside a root passed by trusted code through the
validation context (``Timeline.model_validate(data, context={'allowed_roots': [...]})``).
The legacy ``render_command`` wrapper uses that to render files it was handed directly.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationInfo, field_validator, model_validator

Norm = Annotated[float, Field(ge=0, le=1)]

MAX_DURATION = 180.0
MAX_OVERLAYS = 24          # cards, titles, callouts
MAX_CAPTION_OVERLAYS = 240  # pre-rendered PNG caption groups
REVIEW_LICENSES = {'unknown', 'restricted', 'permission_pending'}
_COLOR = re.compile(r'^(#[0-9A-Fa-f]{6}|[a-z]{3,20})$')


def media_root() -> Path:
    from django.conf import settings
    return Path(settings.MEDIA_ROOT)


def _allowed_roots(info: ValidationInfo | None) -> list[Path]:
    roots = [media_root()]
    context = (info.context if info is not None else None) or {}
    roots += [Path(r) for r in context.get('allowed_roots', ())]
    return roots


def contained_path(path: str, info: ValidationInfo | None) -> str:
    """Return the absolute path to hand to FFmpeg, or raise if it escapes the allowed roots.

    The returned string is not symlink-resolved so argv keeps the caller's spelling, but
    containment is decided on the fully resolved path (symlinks and ``..`` included).
    """
    if not path or '\x00' in path or '\n' in path or '\r' in path:
        raise ValueError('media path is empty or contains control characters')
    roots = _allowed_roots(info)
    candidate = Path(path) if os.path.isabs(path) else roots[0] / path
    real = candidate.resolve()
    if not any(real.is_relative_to(root.resolve()) for root in roots):
        raise ValueError('media path escapes MEDIA_ROOT')
    return str(candidate)


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Canvas(_Model):
    width: int = Field(1080, ge=16, le=4096)
    height: int = Field(1920, ge=16, le=4096)
    fps: float = Field(30, gt=0, le=120)

    @field_validator('width', 'height')
    @classmethod
    def _even(cls, value):
        if value % 2:
            raise ValueError('canvas dimensions must be even for yuv420p')
        return value

    @property
    def frame(self) -> float:
        return 1 / self.fps


class MediaRef(_Model):
    kind: Literal['source', 'asset', 'generated', 'color']
    path: str | None = None
    asset_id: UUID | None = None
    duration: float = Field(gt=0)
    has_audio: bool = False
    width: int | None = Field(None, gt=0, le=8192)
    height: int | None = Field(None, gt=0, le=8192)
    license_status: str = 'unknown'
    color: str = 'black'          # only for kind='color'
    _abs: str | None = PrivateAttr(None)

    @model_validator(mode='after')
    def _contain(self, info: ValidationInfo):
        if self.kind == 'color':
            if not _COLOR.match(self.color):
                raise ValueError('color must be #RRGGBB or a lowercase FFmpeg colour name')
            if self.has_audio:
                raise ValueError('color media has no audio')
            return self
        if not self.path:
            raise ValueError(f'{self.kind} media requires a path')
        self._abs = contained_path(self.path, info)
        return self

    @property
    def abs_path(self) -> str | None:
        return self._abs


class Rect(_Model):
    x: int
    y: int
    w: int = Field(gt=0)
    h: int = Field(gt=0)

    def inside(self, other_w: int, other_h: int, ox: int = 0, oy: int = 0) -> bool:
        return self.x >= ox and self.y >= oy and self.x + self.w <= ox + other_w and self.y + self.h <= oy + other_h

    def within(self, other: 'Rect') -> bool:
        return self.inside(other.w, other.h, other.x, other.y)


class CropKey(_Model):
    t: float = Field(ge=0)
    cx: Norm
    cy: Norm


class ZoomKey(_Model):
    t: float = Field(ge=0)
    zoom: float = Field(ge=1, le=1.6)
    mode: Literal['cut', 'ease'] = 'cut'


def _norm_rect(value, name):
    if value is None:
        return value
    x, y, w, h = value
    if w <= 0 or h <= 0 or x + w > 1 + 1e-6 or y + h > 1 + 1e-6:
        raise ValueError(f'{name} must be a non-empty normalised rect inside the frame')
    return value


class Crop(_Model):
    mode: Literal['center', 'fixed', 'path', 'fit_blur', 'letterbox'] = 'center'
    # The crop window always takes the slot's aspect so nothing is stretched; ``aspect``
    # records the intended aspect for producers and is checked against the slot by QA only.
    aspect: float = Field(9 / 16, gt=0)
    region: tuple[Norm, Norm, Norm, Norm] | None = None
    keys: list[CropKey] = Field(default_factory=list, max_length=32)
    safe_area: tuple[Norm, Norm, Norm, Norm] | None = None

    @model_validator(mode='after')
    def _check(self):
        _norm_rect(self.region, 'region')
        _norm_rect(self.safe_area, 'safe_area')
        if self.mode == 'fixed' and self.region is None:
            raise ValueError('fixed crop requires a region')
        if self.mode == 'path':
            if not self.keys:
                raise ValueError('path crop requires at least one key')
            if any(b.t <= a.t for a, b in zip(self.keys, self.keys[1:])):
                raise ValueError('crop keys must have strictly increasing times')
        return self


class Transition(_Model):
    kind: Literal['cut', 'dip', 'crossfade'] = 'cut'
    duration: float = Field(0, ge=0, le=1)


class VideoClip(_Model):
    id: str
    media: MediaRef
    src_in: float = Field(ge=0)
    src_out: float
    start: float = Field(ge=0)
    crop: Crop = Field(default_factory=Crop)
    zoom: list[ZoomKey] = Field(default_factory=list, max_length=32)
    still: bool = False
    grade: Literal['none', 'restrained', 'punchy', 'filmic'] = 'restrained'
    transition_in: Transition = Field(default_factory=Transition)

    @property
    def length(self) -> float:
        return self.src_out - self.src_in

    @property
    def end(self) -> float:
        return self.start + self.length

    @model_validator(mode='after')
    def _check(self):
        if self.src_out <= self.src_in:
            raise ValueError(f'clip {self.id}: src_out must be greater than src_in')
        if not self.still and self.media.kind != 'color' and self.src_out > self.media.duration + 0.05:
            raise ValueError(f'clip {self.id}: src_out exceeds media duration')
        if any(b.t <= a.t for a, b in zip(self.zoom, self.zoom[1:])):
            raise ValueError(f'clip {self.id}: zoom keys must have strictly increasing times')
        if self.zoom and self.zoom[-1].t > self.length + 1e-6:
            raise ValueError(f'clip {self.id}: zoom key beyond clip length')
        if self.crop.keys and self.crop.keys[-1].t > self.length + 1e-6:
            raise ValueError(f'clip {self.id}: crop key beyond clip length')
        if self.transition_in.kind != 'cut' and self.transition_in.duration >= self.length:
            raise ValueError(f'clip {self.id}: transition longer than clip')
        return self


class Slot(_Model):
    name: str = Field(pattern=r'^[a-z][a-z0-9_]{0,30}$')
    rect: Rect
    z: int = 0
    border_radius: int = Field(0, ge=0)   # not rendered yet; kept for producers


class Layout(_Model):
    kind: Literal['single', 'blur_fill', 'split_v', 'pip', 'letterbox']
    slots: list[Slot] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def _check(self):
        names = [s.name for s in self.slots]
        if len(set(names)) != len(names):
            raise ValueError('slot names must be unique')
        if self.kind in ('split_v', 'pip') and len(self.slots) < 2:
            raise ValueError(f'{self.kind} layout needs at least two slots')
        return self

    def slot(self, name) -> Slot:
        return next(s for s in self.slots if s.name == name)


class VideoTrack(_Model):
    slot: str
    clips: list[VideoClip] = Field(min_length=1)

    @model_validator(mode='after')
    def _ordered(self):
        for a, b in zip(self.clips, self.clips[1:]):
            if b.start < a.start:
                raise ValueError(f'track {self.slot}: clips must be sorted by start')
            if b.start < a.end - 1e-3:
                raise ValueError(f'track {self.slot}: clips {a.id} and {b.id} overlap')
        return self


class Overlay(_Model):
    id: str
    kind: Literal['hook_title', 'fact_card', 'comparison_card', 'callout', 'numbered_list',
                  'title_card', 'image', 'caption']
    png: str | None = None
    frames: str | None = None     # image2 pattern, e.g. counter_%03d.png
    spec: dict = Field(default_factory=dict)
    start: float = Field(ge=0)
    end: float
    rect: Rect
    animation: Literal['none', 'fade', 'slide_up', 'pop'] = 'fade'
    z: int = 10
    license_status: str = 'generated'
    _abs: str | None = PrivateAttr(None)

    @model_validator(mode='after')
    def _check(self, info: ValidationInfo):
        if self.end <= self.start:
            raise ValueError(f'overlay {self.id}: end must be after start')
        if self.png and self.frames:
            raise ValueError(f'overlay {self.id}: use png or frames, not both')
        if self.png or self.frames:
            self._abs = contained_path(self.png or self.frames, info)
        return self

    @property
    def abs_path(self) -> str | None:
        return self._abs


class Word(_Model):
    start: float
    end: float
    text: str


class Cue(_Model):
    start: float = Field(ge=0)
    end: float
    text: str
    translated_english: str = ''
    translated_hindi: str = ''
    words: list[Word] | None = None
    detected_language: str = 'und'

    @model_validator(mode='after')
    def _check(self):
        if self.end <= self.start:
            raise ValueError('cue end must be after start')
        return self


class CaptionTrack(_Model):
    preset: Literal['bold_gaming', 'cinematic', 'dynamic_creator', 'clean_commentary', 'bilingual'] = 'clean_commentary'
    mode: Literal['original', 'english', 'hindi', 'bilingual'] = 'original'
    cues: list[Cue] = Field(default_factory=list)
    renderer: Literal['auto', 'ass', 'png'] = 'auto'
    safe_rect: Rect
    ass_file: str | None = None   # prebuilt .ass (legacy captions); basename must be filter-safe
    _abs: str | None = PrivateAttr(None)

    @model_validator(mode='after')
    def _check(self, info: ValidationInfo):
        if self.ass_file:
            self._abs = contained_path(self.ass_file, info)
            if not re.fullmatch(r'[A-Za-z0-9_.-]+\.ass', Path(self._abs).name):
                raise ValueError('ass_file basename must be [A-Za-z0-9_.-]+.ass')
        return self

    @property
    def abs_path(self) -> str | None:
        return self._abs


class AudioSegment(_Model):
    id: str
    media: MediaRef
    src_in: float = Field(ge=0)
    src_out: float
    start: float = Field(ge=0)
    role: Literal['source', 'narration', 'music', 'sfx']
    gain_db: float = Field(0, ge=-60, le=20)
    fade_in: float = Field(.015, ge=0)
    fade_out: float = Field(.015, ge=0)
    duck_under: list[Literal['source', 'narration', 'music', 'sfx']] = Field(default_factory=list)

    @property
    def length(self) -> float:
        return self.src_out - self.src_in

    @property
    def end(self) -> float:
        return self.start + self.length

    @model_validator(mode='after')
    def _check(self):
        if self.src_out <= self.src_in:
            raise ValueError(f'audio {self.id}: src_out must be greater than src_in')
        if not self.media.has_audio:
            raise ValueError(f'audio {self.id}: media has no audio stream')
        if self.src_out > self.media.duration + 0.05:
            raise ValueError(f'audio {self.id}: src_out exceeds media duration')
        if self.fade_in + self.fade_out > self.length + 1e-6:
            raise ValueError(f'audio {self.id}: fades longer than segment')
        if self.role in self.duck_under:
            raise ValueError(f'audio {self.id}: a role cannot duck under itself')
        return self


class Timeline(_Model):
    version: Literal[1] = 1
    canvas: Canvas = Field(default_factory=Canvas)
    layout: Layout
    video: list[VideoTrack] = Field(min_length=1)
    overlays: list[Overlay] = Field(default_factory=list)
    captions: CaptionTrack | None = None
    audio: list[AudioSegment] = Field(default_factory=list)
    loudness_lufs: float = Field(-16, ge=-30, le=-8)
    duration: float = Field(gt=0, le=MAX_DURATION)
    fade_in: float = Field(0, ge=0, le=2)     # whole-programme fade from/to black
    fade_out: float = Field(0, ge=0, le=2)
    provenance: list[dict] = Field(default_factory=list)   # derived; any input is replaced

    @model_validator(mode='after')
    def _check(self):
        cw, ch, frame = self.canvas.width, self.canvas.height, self.canvas.frame
        tol = frame + 1e-6
        names = {s.name for s in self.layout.slots}
        for slot in self.layout.slots:
            if not slot.rect.inside(cw, ch):
                raise ValueError(f'slot {slot.name} lies outside the canvas')
            if slot.rect.w % 2 or slot.rect.h % 2:
                raise ValueError(f'slot {slot.name} dimensions must be even')
        for track in self.video:
            if track.slot not in names:
                raise ValueError(f'track refers to unknown slot {track.slot}')
        if len({t.slot for t in self.video}) != len(self.video):
            raise ValueError('one track per slot')
        end = max(c.end for t in self.video for c in t.clips)
        if abs(end - self.duration) > tol:
            raise ValueError(f'duration {self.duration:.3f}s does not match last clip end {end:.3f}s')
        background = 'background' in names
        for track in self.video:
            slot = self.layout.slot(track.slot)
            if slot.z != 0 or background:
                continue
            cursor = 0.0
            for clip in track.clips:
                if clip.start - cursor > tol:
                    raise ValueError(f'track {track.slot} has a gap at {cursor:.3f}s (no background slot)')
                cursor = clip.end
            if self.duration - cursor > tol:
                raise ValueError(f'track {track.slot} ends before the timeline')
        cards = [o for o in self.overlays if o.kind != 'caption']
        if len(cards) > MAX_OVERLAYS:
            raise ValueError(f'at most {MAX_OVERLAYS} overlays')
        if len(self.overlays) - len(cards) > MAX_CAPTION_OVERLAYS:
            raise ValueError(f'at most {MAX_CAPTION_OVERLAYS} caption overlays')
        if len({o.id for o in self.overlays}) != len(self.overlays):
            raise ValueError('overlay ids must be unique')
        for overlay in self.overlays:
            if not overlay.rect.inside(cw, ch):
                raise ValueError(f'overlay {overlay.id} lies outside the canvas')
            if overlay.end > self.duration + tol:
                raise ValueError(f'overlay {overlay.id} ends after the timeline')
        for segment in self.audio:
            if segment.end > self.duration + tol:
                raise ValueError(f'audio {segment.id} ends after the timeline')
        if self.captions:
            if not self.captions.safe_rect.inside(cw, ch):
                raise ValueError('caption safe_rect lies outside the canvas')
            if any(c.end > self.duration + tol for c in self.captions.cues):
                raise ValueError('caption cue ends after the timeline')
        if self.fade_in + self.fade_out > self.duration:
            raise ValueError('programme fades longer than the timeline')
        self.provenance = self._derive_provenance()
        return self

    def _derive_provenance(self) -> list[dict]:
        seen, out = set(), []
        refs = [c.media for t in self.video for c in t.clips] + [a.media for a in self.audio]
        for ref in refs:
            if ref.kind == 'color':
                continue
            key = (ref.path, str(ref.asset_id or ''))
            if key in seen:
                continue
            seen.add(key)
            out.append({'kind': ref.kind, 'path': ref.path, 'asset_id': str(ref.asset_id) if ref.asset_id else None,
                        'license_status': ref.license_status})
        for overlay in self.overlays:
            if overlay.png or overlay.frames:
                key = (overlay.png or overlay.frames, '')
                if key not in seen:
                    seen.add(key)
                    out.append({'kind': 'overlay', 'path': overlay.png or overlay.frames, 'asset_id': None,
                                'license_status': overlay.license_status})
        return out


def provenance_summary(provenance: list[dict]) -> dict:
    statuses: dict[str, int] = {}
    for item in provenance:
        statuses[item.get('license_status') or 'unknown'] = statuses.get(item.get('license_status') or 'unknown', 0) + 1
    review = sorted(s for s in statuses if s in REVIEW_LICENSES)
    return {'statuses': statuses, 'items': len(provenance), 'review_required': bool(review),
            'review_reasons': review,
            'rights_review_status': 'manual_review_required' if review else 'not_required'}
