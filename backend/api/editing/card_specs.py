"""Pydantic specs and themes for Pillow-rendered motion graphics cards (WP9).

Pure data: no Pillow import here, so the timeline / API layers can validate card
payloads without loading fonts. Rendering lives in ``graphics.py``.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


def _text(max_length):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=max_length)]


class _Spec(BaseModel):
    model_config = ConfigDict(extra='forbid')


class LabelValue(_Spec):
    label: _text(32)
    value: _text(28)


class FactCard(_Spec):
    kind: Literal['fact_card'] = 'fact_card'
    headline: _text(90)
    body: _text(240)
    source_label: _text(48) | None = None


class ComparisonCard(_Spec):
    kind: Literal['comparison_card'] = 'comparison_card'
    left: LabelValue
    right: LabelValue
    verdict: _text(80) | None = None


class Callout(_Spec):
    kind: Literal['callout'] = 'callout'
    text: _text(90)
    anchor: tuple[float, float] = (0.5, 0.5)  # normalised x, y of the point being called out

    @field_validator('anchor')
    @classmethod
    def _norm(cls, value):
        if not all(0.0 <= v <= 1.0 for v in value):
            raise ValueError('anchor must be normalised to 0..1')
        return value


class NumberedList(_Spec):
    kind: Literal['numbered_list'] = 'numbered_list'
    items: list[_text(72)] = Field(min_length=1, max_length=5)
    reveal: Literal['stagger', 'all'] = 'stagger'


class TitleCard(_Spec):
    kind: Literal['title_card'] = 'title_card'
    title: _text(72)
    kicker: _text(32) | None = None


class HookTitle(_Spec):
    kind: Literal['hook_title'] = 'hook_title'
    text: _text(80)


CardSpec = FactCard | ComparisonCard | Callout | NumberedList | TitleCard | HookTitle

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


class Theme(_Spec):
    """Restrained look: near-black panel, one accent, no gradients or glows."""
    name: str
    panel: RGBA = (12, 12, 14, 238)
    border: RGBA = (255, 255, 255, 28)
    text: RGB = (245, 245, 245)
    muted: RGB = (168, 168, 174)
    accent: RGB = (255, 214, 10)
    accent_text: RGB = (12, 12, 14)  # text drawn on top of the accent colour
    radius: int = 20
    weight: Literal['regular', 'bold'] = 'bold'  # headline weight


THEMES: dict[str, Theme] = {
    t.name: t for t in (
        Theme(name='gaming_hype', accent=(255, 214, 10), radius=18, weight='bold'),
        Theme(name='cinematic_gaming', panel=(10, 10, 12, 226), accent=(226, 183, 110),
              accent_text=(16, 12, 6), radius=8, weight='regular'),
        Theme(name='viral_commentary', accent=(255, 76, 92), accent_text=(255, 255, 255),
              radius=26, weight='bold'),
        Theme(name='minimal_premium', panel=(18, 18, 20, 240), accent=(236, 236, 236),
              accent_text=(18, 18, 20), radius=14, weight='regular'),
        Theme(name='split_screen_gaming', accent=(0, 214, 160), accent_text=(6, 18, 14),
              radius=12, weight='bold'),
    )
}


def get_theme(theme) -> Theme:
    if isinstance(theme, Theme):
        return theme
    try:
        return THEMES[theme]
    except KeyError:
        raise ValueError(f'unknown theme {theme!r}; choose from {sorted(THEMES)}')
