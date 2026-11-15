"""Pillow motion graphics: transparent PNG cards and PNG captions (WP9).

The local FFmpeg has no libass/drawtext, so all on-screen text is rasterised here
and composited by the FFmpeg compiler with ``overlay``. Devanagari is only ever
rendered with the raqm (HarfBuzz) layout engine; without it ``ensure_shaping``
raises ``StudioError('text_shaping_unavailable')`` instead of drawing broken
conjuncts.

Fonts: bundled Noto Sans / Noto Sans Devanagari (SIL OFL) under ``api/assets/fonts``.
Set ``CAPTION_FONT_PATH`` to a .ttf/.ttc to override both scripts (for example
``/System/Library/Fonts/Supplemental/Arial Unicode.ttf``).
"""
from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, features

from api.services.media import StudioError

from .card_specs import (Callout, ComparisonCard, FactCard, HookTitle, NumberedList,
                         Theme, TitleCard, get_theme)

FONT_DIR = Path(__file__).resolve().parent.parent / 'assets' / 'fonts'
DEFAULT_CANVAS = (1080, 1920)
# Shorts/Reels UI keep-out at 1080x1920 (scaled for other canvases).
SAFE_TOP, SAFE_BOTTOM, SAFE_LEFT, SAFE_RIGHT = 220, 420, 60, 140


# ---------------------------------------------------------------- shaping guard

def has_devanagari(text) -> bool:
    return any('ऀ' <= c <= 'ॿ' for c in text)


def raqm_available() -> bool:
    return bool(features.check('raqm'))


def ensure_shaping(text):
    """Refuse to render Devanagari with basic layout (broken matras/conjuncts)."""
    if has_devanagari(text) and not features.check('raqm'):
        raise StudioError('text_shaping_unavailable',
                          'Install fribidi (brew install fribidi) so Devanagari renders correctly')


# ------------------------------------------------------------------------ fonts

def _font_file(deva, bold):
    override = os.getenv('CAPTION_FONT_PATH')
    if override and Path(override).exists():
        return override
    name = ('NotoSansDevanagari' if deva else 'NotoSans') + ('-Bold' if bold else '-Regular') + '.ttf'
    path = FONT_DIR / name
    if not path.exists():
        raise StudioError('font_missing', f'Bundled font {name} is missing; set CAPTION_FONT_PATH to a .ttf file.')
    return str(path)


@lru_cache(maxsize=256)
def _font(deva, bold, size, basic=False):
    engine = ImageFont.Layout.BASIC if (basic or not features.check('raqm')) else ImageFont.Layout.RAQM
    return ImageFont.truetype(_font_file(deva, bold), int(size), layout_engine=engine)


def _runs(text):
    """Split text into (is_devanagari, segment); neutral characters join the current run."""
    runs, cur, cur_type = [], '', None
    for c in text:
        if 'ऀ' <= c <= 'ॿ':
            t = True
        elif c in '‌‍' or not c.isalpha():
            t = cur_type if cur_type is not None else False
        else:
            t = False
        if cur and t != cur_type:
            runs.append((cur_type, cur))
            cur = ''
        cur_type = t
        cur += c
    if cur:
        runs.append((cur_type, cur))
    return runs


def text_width(text, size, bold):
    return sum(_font(d, bold, size).getlength(seg) for d, seg in _runs(text))


def _draw_runs(draw, x, baseline, text, size, bold, fill, stroke_width=0):
    for d, seg in _runs(text):
        f = _font(d, bold, size)
        draw.text((x, baseline), seg, font=f, fill=fill, anchor='ls',
                  stroke_width=stroke_width, stroke_fill=fill)
        x += f.getlength(seg)


# ---------------------------------------------------------------------- layout

@dataclass
class Block:
    size: int
    bold: bool
    lines: list           # list[list[(text, x, w, global_word_index)]]
    line_w: list
    asc: int
    desc: int
    line_h: int
    width: int
    height: int
    truncated: bool = False


def _unbreakable_before(ch):
    return unicodedata.category(ch).startswith('M') or ch in '‌‍'


def _split_long(word, size, bold, max_w):
    """Last resort: break a word wider than the box without splitting a grapheme."""
    parts, cur = [], ''
    for ch in word:
        trial = cur + ch
        if cur and text_width(trial, size, bold) > max_w and not _unbreakable_before(ch) \
                and not cur.endswith('्'):
            parts.append(cur)
            cur = ch
        else:
            cur = trial
    return parts + ([cur] if cur else [])


def _metrics(text, size, bold):
    fonts = [_font(False, bold, size)] + ([_font(True, bold, size)] if has_devanagari(text) else [])
    asc = max(f.getmetrics()[0] for f in fonts)
    desc = max(f.getmetrics()[1] for f in fonts)
    leading = 1.0 if has_devanagari(text) else 0.92
    return asc, desc, max(1, round((asc + desc) * leading))


def _wrap(words, size, bold, max_w, split_long):
    space = _font(False, bold, size).getlength(' ')
    lines, cur, cur_w = [], [], 0.0
    for idx, word in words:
        w = text_width(word, size, bold)
        if w > max_w and split_long:
            pieces = _split_long(word, size, bold, max_w)
        else:
            pieces = [word]
        for p_i, piece in enumerate(pieces):
            pw = text_width(piece, size, bold)
            add = pw if not cur else space + pw
            if cur and cur_w + add > max_w:
                lines.append(cur)
                cur, cur_w = [], 0.0
                add = pw
            cur.append((piece, idx))
            cur_w += add
    if cur:
        lines.append(cur)
    return lines


def _build_block(text, lines, size, bold, truncated=False):
    space = _font(False, bold, size).getlength(' ')
    asc, desc, line_h = _metrics(text, size, bold)
    out, widths = [], []
    for line in lines:
        x, row = 0.0, []
        for i, (piece, idx) in enumerate(line):
            if i:
                x += space
            w = text_width(piece, size, bold)
            row.append((piece, x, w, idx))
            x += w
        out.append(row)
        widths.append(x)
    n = max(1, len(out))
    return Block(size, bold, out, widths, asc, desc, line_h, int(round(max(widths, default=0))),
                 (n - 1) * line_h + asc + desc, truncated)


def fit_text(text, max_w, max_h, max_size, min_size, bold=True, max_lines=3, step=2):
    """Largest font size (<= max_size) whose wrapped text fits max_w x max_h."""
    ensure_shaping(text)
    text = ' '.join(text.split())
    words = list(enumerate(text.split()))
    max_w, max_h = max(1, int(max_w)), max(1, int(max_h))
    size = int(max_size)
    while True:
        at_min = size <= min_size
        size = max(size, int(min_size))
        lines = _wrap(words, size, bold, max_w, split_long=at_min)
        block = _build_block(text, lines, size, bold)
        if len(lines) <= max_lines and block.height <= max_h and block.width <= max_w:
            return block
        if at_min:
            break
        size -= step
    # Cannot fit even at the minimum size: shrink further, then ellipsise as a last resort.
    size = int(min_size)
    while size > 12:
        size -= 2
        lines = _wrap(words, size, bold, max_w, split_long=True)
        block = _build_block(text, lines, size, bold)
        if len(lines) <= max_lines and block.height <= max_h and block.width <= max_w:
            return block
    lines = _wrap(words, size, bold, max_w, split_long=True)[:max_lines]
    last = ''.join(p for p, _ in lines[-1]) if lines else ''
    while last and text_width(last + '…', size, bold) > max_w:
        last = last[:-1]
    lines[-1] = [(last + '…', lines[-1][0][1])] if lines else []
    return _build_block(text, lines, size, bold, truncated=True)


def _text_mask(size, items, stroke=0, offset=(0, 0)):
    mask = Image.new('L', size, 0)
    d = ImageDraw.Draw(mask)
    for x, baseline, text, fsize, bold in items:
        _draw_runs(d, x + offset[0], baseline + offset[1], text, fsize, bold, 255, stroke)
    return mask


def _composite(img, mask, color, alpha=255):
    if alpha != 255:
        mask = mask.point(lambda v: v * alpha // 255)
    layer = Image.new('RGBA', img.size, tuple(color[:3]) + (255,))
    layer.putalpha(mask)
    img.alpha_composite(layer)


def draw_block(img, block, x, y, box_w, align, fill, stroke=0, stroke_fill=(0, 0, 0),
               shadow=None, highlight_idx=None, highlight=None, upper=False):
    """Draw a laid-out block into img (RGBA). Returns nothing; text is composited with proper alpha."""
    groups = {'base': [], 'hi': []}
    all_items = []
    for li, row in enumerate(block.lines):
        baseline = y + li * block.line_h + block.asc
        lw = block.line_w[li]
        x0 = x if align == 'left' else x + (box_w - lw) / 2
        for text, wx, _w, idx in row:
            item = (x0 + wx, baseline, text.upper() if upper else text, block.size, block.bold)
            all_items.append(item)
            groups['hi' if highlight_idx is not None and idx == highlight_idx else 'base'].append(item)
    if shadow:
        (dx, dy), scol, sal = shadow
        _composite(img, _text_mask(img.size, all_items, stroke, (dx, dy)), scol, sal)
    if stroke:
        _composite(img, _text_mask(img.size, all_items, stroke), stroke_fill)
    _composite(img, _text_mask(img.size, groups['base']), fill)
    if groups['hi']:
        _composite(img, _text_mask(img.size, groups['hi']), highlight or fill)


# ----------------------------------------------------------------- panel helper

def _panel(w, h, theme: Theme, radius, accent_bar=True, panel=True):
    ss = 3
    big = Image.new('RGBA', (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    if panel:
        d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius * ss, fill=theme.panel)
        d.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius * ss, outline=theme.border, width=ss)
    if accent_bar and panel:
        bar = 8 * ss
        inset = max(radius, 14) * ss * 0.6
        d.rounded_rectangle((inset * 0.5 + 6 * ss, inset, inset * 0.5 + 6 * ss + bar, h * ss - inset),
                            bar // 2, fill=theme.accent + (255,))
    return big.resize((w, h), Image.LANCZOS)


# ------------------------------------------------------------------ canvas math

def _scales(canvas):
    return canvas[0] / 1080, canvas[1] / 1920


def safe_rect(canvas=DEFAULT_CANVAS):
    """(left, top, right, bottom) keep-out-aware safe box in canvas pixels."""
    kx, ky = _scales(canvas)
    return (round(SAFE_LEFT * kx), round(SAFE_TOP * ky),
            round(canvas[0] - SAFE_RIGHT * kx), round(canvas[1] - SAFE_BOTTOM * ky))


def _clamp_rect(x, y, w, h, canvas):
    l, t, r, b = safe_rect(canvas)
    x = min(max(x, l), max(l, r - w))
    y = min(max(y, t), max(t, b - h))
    return int(x), int(y), int(w), int(h)


@dataclass
class CardRender:
    kind: str
    paths: list           # list[Path]; several only for staggered NumberedList reveals
    rect: tuple           # (x, y, w, h) where the PNG(s) should be overlaid on the canvas
    size: tuple           # (w, h) of the PNG(s)
    reveal: str = 'none'  # 'stagger' when paths are cumulative reveal steps
    meta: dict = field(default_factory=dict)

    @property
    def path(self):
        return self.paths[-1]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _key(*parts):
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _heading_bold(theme):
    return theme.weight == 'bold'


# ------------------------------------------------------------------------ cards

def _fact(spec: FactCard, theme, canvas):
    kx, ky = _scales(canvas)
    l, t, r, b = safe_rect(canvas)
    w = r - l
    pad, bar = round(44 * kx), round(30 * kx)
    inner_w = w - pad - bar - round(8 * kx)
    max_h = (b - t) - 2 * pad
    head = fit_text(spec.headline, inner_w, max_h * .4, 64 * kx, 36 * kx, _heading_bold(theme), 3)
    body = fit_text(spec.body, inner_w, max_h * .5, 40 * kx, 26 * kx, False, 6)
    src = fit_text(f'Source: {spec.source_label}', inner_w, 60 * kx, 28 * kx, 18 * kx, False, 1) \
        if spec.source_label else None
    gap = round(22 * kx)
    h = pad + head.height + gap + body.height + (gap + src.height if src else 0) + pad
    img = _panel(w, h, theme, round(theme.radius * kx))
    tx, y = bar + round(8 * kx), pad
    draw_block(img, head, tx, y, inner_w, 'left', theme.text)
    y += head.height + gap
    draw_block(img, body, tx, y, inner_w, 'left', theme.muted)
    y += body.height + gap
    if src:
        draw_block(img, src, tx, y, inner_w, 'left', theme.accent)
    return [img], _clamp_rect(l, b - h - round(20 * ky), w, h, canvas)


def _comparison(spec: ComparisonCard, theme, canvas):
    kx, ky = _scales(canvas)
    l, t, r, b = safe_rect(canvas)
    w = r - l
    pad, divider = round(40 * kx), round(2 * kx)
    col_w = (w - 2 * pad - divider) // 2 - round(24 * kx)
    sides = []
    for side in (spec.left, spec.right):
        sides.append((fit_text(side.label, col_w, 120 * kx, 30 * kx, 20 * kx, False, 2),
                      fit_text(side.value, col_w, 180 * kx, 76 * kx, 34 * kx, True, 2)))
    gap = round(16 * kx)
    row_h = max(lb.height + gap + vb.height for lb, vb in sides)
    verdict = fit_text(spec.verdict, w - 2 * pad, 140 * kx, 40 * kx, 26 * kx, True, 2) if spec.verdict else None
    h = pad + row_h + (round(30 * kx) + verdict.height if verdict else 0) + pad
    img = _panel(w, h, theme, round(theme.radius * kx), accent_bar=False)
    d = ImageDraw.Draw(img)
    cx = w // 2
    d.rectangle((cx - divider // 2, pad, cx + divider // 2, pad + row_h), fill=theme.border[:3] + (90,))
    for i, (lb, vb) in enumerate(sides):
        x0 = pad if i == 0 else cx + divider // 2 + round(24 * kx)
        draw_block(img, lb, x0, pad, col_w, 'center', theme.muted)
        draw_block(img, vb, x0, pad + lb.height + gap, col_w, 'center', theme.text if i == 0 else theme.accent)
    if verdict:
        draw_block(img, verdict, pad, pad + row_h + round(30 * kx), w - 2 * pad, 'center', theme.text)
    return [img], _clamp_rect(l, round(canvas[1] * .40 - h / 2), w, h, canvas)


def _callout(spec: Callout, theme, canvas):
    kx, ky = _scales(canvas)
    l, t, r, b = safe_rect(canvas)
    pad = round(30 * kx)
    max_w = min(round(620 * kx), r - l)
    inner_w = max_w - 2 * pad - round(14 * kx)
    block = fit_text(spec.text, inner_w, 260 * kx, 44 * kx, 28 * kx, _heading_bold(theme), 3)
    w = block.width + 2 * pad + round(14 * kx)
    h = block.height + 2 * pad
    img = _panel(w, h, theme, round(theme.radius * kx * .8))
    draw_block(img, block, pad + round(14 * kx), pad, block.width, 'left', theme.text)
    ax, ay = spec.anchor
    return [img], _clamp_rect(ax * canvas[0] - w / 2, ay * canvas[1] - h / 2, w, h, canvas)


def _numbered(spec: NumberedList, theme, canvas):
    kx, ky = _scales(canvas)
    l, t, r, b = safe_rect(canvas)
    w = r - l
    pad, badge, gap = round(36 * kx), round(72 * kx), round(22 * kx)
    text_w = w - 2 * pad - badge - gap
    max_row_h = ((b - t) - 2 * pad) / len(spec.items) - round(14 * kx)
    size = 46 * kx
    blocks = [fit_text(i, text_w, max_row_h, size, 26 * kx, _heading_bold(theme), 2) for i in spec.items]
    size = min(bl.size for bl in blocks)  # one shared size keeps the list visually even
    blocks = [fit_text(i, text_w, max_row_h, size, 20 * kx, _heading_bold(theme), 2) for i in spec.items]
    row_h = max(badge, max(bl.height for bl in blocks))
    rgap = round(14 * kx)
    h = pad + len(blocks) * row_h + (len(blocks) - 1) * rgap + pad

    def frame(visible):
        img = _panel(w, h, theme, round(theme.radius * kx), accent_bar=False)
        for n in range(visible):
            y = pad + n * (row_h + rgap)
            bs = 4
            circle = Image.new('RGBA', (badge * bs, badge * bs), (0, 0, 0, 0))
            ImageDraw.Draw(circle).ellipse((0, 0, badge * bs - 1, badge * bs - 1), fill=theme.accent + (255,))
            img.alpha_composite(circle.resize((badge, badge), Image.LANCZOS), (pad, y + (row_h - badge) // 2))
            num = fit_text(str(n + 1), badge * .6, badge * .8, badge * .55, 20, True, 1)
            draw_block(img, num, pad + (badge - num.width) / 2, y + (row_h - badge) // 2 + (badge - num.height) / 2,
                       num.width, 'left', theme.accent_text)
            draw_block(img, blocks[n], pad + badge + gap, y + (row_h - blocks[n].height) // 2,
                       text_w, 'left', theme.text)
        return img

    count = len(blocks)
    steps = list(range(1, count + 1)) if spec.reveal == 'stagger' else [count]
    return [frame(n) for n in steps], _clamp_rect(l, round(canvas[1] * .40 - h / 2), w, h, canvas)


def _title(spec: TitleCard, theme, canvas):
    kx, ky = _scales(canvas)
    l, t, r, b = safe_rect(canvas)
    w = r - l
    pad = round(52 * kx)
    inner_w = w - 2 * pad
    kick = fit_text(spec.kicker.upper(), inner_w, 60 * kx, 30 * kx, 20 * kx, True, 1) if spec.kicker else None
    title = fit_text(spec.title, inner_w, (b - t) * .5, 88 * kx, 40 * kx, _heading_bold(theme), 3)
    gap, rule = round(22 * kx), round(6 * kx)
    h = pad + (kick.height + gap if kick else 0) + title.height + gap + rule + pad
    img = _panel(w, h, theme, round(theme.radius * kx), accent_bar=False)
    y = pad
    if kick:
        draw_block(img, kick, pad, y, inner_w, 'center', theme.accent)
        y += kick.height + gap
    draw_block(img, title, pad, y, inner_w, 'center', theme.text)
    y += title.height + gap
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((w // 2 - round(48 * kx), y, w // 2 + round(48 * kx), y + rule), rule // 2,
                        fill=theme.accent + (255,))
    return [img], _clamp_rect(l, round(canvas[1] * .40 - h / 2), w, h, canvas)


def _hook(spec: HookTitle, theme, canvas):
    kx, ky = _scales(canvas)
    l, t, r, b = safe_rect(canvas)
    pad = round(36 * kx)
    inner_w = (r - l) - 2 * pad
    block = fit_text(spec.text, inner_w, 380 * kx, 80 * kx, 36 * kx, True, 3)
    w = max(block.width + 2 * pad, round(300 * kx))
    h = block.height + 2 * pad + round(14 * kx)
    img = _panel(w, h, theme, round(theme.radius * kx), accent_bar=False)
    draw_block(img, block, pad, pad, w - 2 * pad, 'center', theme.text)
    d = ImageDraw.Draw(img)
    rule = round(6 * kx)
    d.rounded_rectangle((pad, h - pad // 2 - rule, w - pad, h - pad // 2), rule // 2, fill=theme.accent + (255,))
    return [img], _clamp_rect(round((canvas[0] - w) / 2), t + round(20 * ky), w, h, canvas)


_RENDERERS = {FactCard: _fact, ComparisonCard: _comparison, Callout: _callout,
              NumberedList: _numbered, TitleCard: _title, HookTitle: _hook}


def render_card(spec, theme, canvas=DEFAULT_CANVAS, out_dir='.') -> CardRender:
    """Render a card spec to RGBA PNG(s) plus the canvas rect to overlay them at."""
    theme = get_theme(theme)
    renderer = _RENDERERS.get(type(spec))
    if renderer is None:
        raise StudioError('unknown_card', f'Unsupported card spec {type(spec).__name__}.')
    for s in _strings(spec.model_dump()):
        ensure_shaping(s)
    images, rect = renderer(spec, theme, tuple(canvas))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = _key(spec.model_dump(), theme.model_dump(), canvas)
    paths = []
    for i, img in enumerate(images):
        path = out / (f'{spec.kind}_{key}.png' if len(images) == 1 else f'{spec.kind}_{key}_{i + 1}.png')
        img.save(path, 'PNG')
        paths.append(path)
    return CardRender(spec.kind, paths, rect, images[0].size,
                      'stagger' if len(images) > 1 else 'none', {'theme': theme.name})


# --------------------------------------------------------------------- captions

@dataclass(frozen=True)
class CaptionPreset:
    name: str
    size: int
    min_size: int
    bold: bool
    fill: tuple
    highlight: tuple
    stroke: int = 0
    shadow: tuple | None = None       # ((dx, dy), colour, alpha)
    upper: bool = False
    max_lines: int = 3
    panel: tuple | None = None        # RGBA pill behind text
    bottom: int = 1480                # baseline limit (px at 1080x1920) for the bottom of the caption
    max_w: int = 800                  # centred on the canvas, so it clears the right 140 px UI strip


PRESETS = {p.name: p for p in (
    CaptionPreset('bold_gaming', 78, 40, True, (255, 221, 0), (255, 255, 255), stroke=6, upper=True,
                  max_lines=3, bottom=1470),
    CaptionPreset('cinematic', 58, 34, False, (232, 232, 232), (240, 196, 130), shadow=((2, 3), (0, 0, 0), 170),
                  max_lines=2, bottom=1490),
    CaptionPreset('dynamic_creator', 76, 40, True, (255, 255, 255), (187, 255, 102), stroke=5, upper=True,
                  max_lines=2, bottom=1470),
    CaptionPreset('clean_commentary', 60, 34, True, (255, 255, 255), (255, 214, 10),
                  panel=(10, 10, 12, 208), max_lines=3, bottom=1480),
    CaptionPreset('bilingual', 64, 30, True, (255, 255, 255), (255, 214, 10), stroke=4,
                  shadow=((2, 3), (0, 0, 0), 150), max_lines=2, bottom=1480),
)}

# Legacy ASS style names accepted by the existing caption_style Literal.
LEGACY_STYLE_MAP = {'gaming': 'bold_gaming', 'cinematic': 'cinematic', 'dynamic': 'dynamic_creator',
                    'minimal': 'clean_commentary'}


def get_preset(name) -> CaptionPreset:
    name = LEGACY_STYLE_MAP.get(name, name)
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(f'unknown caption preset {name!r}; choose from {sorted(PRESETS)}')


def _caption_layout(text, preset, mode, canvas):
    """Return (primary Block, secondary Block|None, uppercase flag) fitted to the preset."""
    kx, ky = _scales(canvas)
    max_w = round(preset.max_w * kx)
    pad_h = round(34 * kx) if preset.panel else 0
    avail_w = max_w - 2 * pad_h
    max_h = round(((canvas[1] - SAFE_BOTTOM * ky) - SAFE_TOP * ky) * .35)
    bilingual = preset.name == 'bilingual' or mode == 'bilingual'
    parts = [p.strip() for p in text.split('\n') if p.strip()]
    pad = preset.stroke + 4
    if bilingual and len(parts) >= 2:
        first, second = parts[0], ' '.join(parts[1:])
        gap = round(24 * kx)
        for s1 in range(int(preset.size * kx), int(preset.min_size * kx) - 1, -2):
            s2 = round(s1 * 0.7)
            a = fit_text(first, avail_w - 2 * pad, max_h, s1, s1, True, 2)
            b = fit_text(second, avail_w - 2 * pad, max_h, s2, s2, False, 2)
            if a.size == s1 and b.size == s2 and not a.truncated and not b.truncated \
                    and a.height + gap + b.height <= max_h:
                return a, b, False
        a = fit_text(first, avail_w - 2 * pad, max_h, preset.min_size * kx, 12, True, 2)
        b = fit_text(second, avail_w - 2 * pad, max_h, round(a.size * .7), 10, False, 2)
        return a, b, False
    flat = ' '.join(parts)
    shown = flat.upper() if preset.upper else flat
    block = fit_text(shown, avail_w - 2 * pad, max_h, preset.size * kx, preset.min_size * kx,
                     preset.bold, preset.max_lines)
    return block, None, preset.upper


def _render_caption(text, preset, mode, canvas, highlight_idx=None):
    kx, ky = _scales(canvas)
    a, b, upper = _caption_layout(text, preset, mode, canvas)
    pad_x = round(34 * kx) if preset.panel else 0
    pad_y = round(22 * kx) if preset.panel else 0
    edge = preset.stroke + 8 + (4 if preset.shadow else 0)   # room for stroke/shadow so nothing clips
    gap = round(24 * kx)
    divider = round(18 * kx) if b else 0
    content_w = max(a.width, b.width if b else 0)
    w = content_w + 2 * (pad_x + edge)
    h = a.height + (gap + b.height + divider if b else 0) + 2 * (pad_y + edge)
    img = _panel(w, h, Theme(name='caption', panel=preset.panel or (0, 0, 0, 0), border=(0, 0, 0, 0)),
                 round(24 * kx), accent_bar=False, panel=bool(preset.panel))
    # Upper-casing happened before layout (changes widths), so drawing must not upper() again.
    x0, y0 = pad_x + edge, pad_y + edge
    draw_block(img, a, x0, y0, content_w, 'center', preset.fill, preset.stroke, (0, 0, 0), preset.shadow,
               highlight_idx, preset.highlight)
    if b:
        ly = y0 + a.height + gap // 2
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((w // 2 - round(40 * kx), ly, w // 2 + round(40 * kx), ly + max(2, round(3 * kx))),
                            2, fill=preset.highlight + (200,))
        draw_block(img, b, x0, y0 + a.height + gap + divider, content_w, 'center', (214, 214, 220),
                   max(0, preset.stroke - 2), (0, 0, 0), preset.shadow)
    bottom = min(round(preset.bottom * ky), round(canvas[1] - SAFE_BOTTOM * ky))
    rect = _clamp_rect(round((canvas[0] - w) / 2), bottom - h, w, h, canvas)
    return img, rect, (a, b)


def _word_states(g_start, g_end, timings, n_words):
    """Per-word [start, end) windows tiling the group, or None when timings are unusable."""
    if not timings or len(timings) != n_words:
        return None
    starts = []
    for i, tw in enumerate(timings):
        st = float(tw[1] if not isinstance(tw, dict) else tw['start'])
        starts.append(min(max(st, g_start), g_end))
    starts[0] = g_start
    for i in range(1, len(starts)):
        starts[i] = max(starts[i], starts[i - 1])
    ends = starts[1:] + [g_end]
    return [(s, e) for s, e in zip(starts, ends) if e > s]


def render_caption_pngs(groups, preset, mode, canvas=DEFAULT_CANVAS, out_dir='.', word_timings=None):
    """PNG per phrase group.

    groups: iterable of (start, end, text). Text may contain '\\n' separating original (first)
    and translated (second) lines for the bilingual preset (as ``caption_text`` mode='bilingual' emits).
    word_timings: optional sequence parallel to groups; each entry a list of (word, start, end) (or
    dicts with 'word','start','end') aligned to the group's primary-line words. When supplied (and the
    count matches) one PNG per word state is emitted with that word highlighted, each carrying
    ``word_index``; otherwise one PNG per group with ``word_index`` None.

    Returns list of {start, end, png, rect, word_index}.
    """
    preset = get_preset(preset)
    canvas = tuple(canvas)
    groups = list(groups)
    for _s, _e, text in groups:
        ensure_shaping(text)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results = []

    def save(text, idx):
        key = _key(preset.name, mode, text, idx, canvas)
        path = out / f'cap_{preset.name}_{key}.png'
        img, rect, blocks = _render_caption(text, preset, mode, canvas, idx)
        if not path.exists():
            img.save(path, 'PNG')
        return str(path), rect, blocks

    for gi, (start, end, text) in enumerate(groups):
        if not text or not text.strip() or end <= start:
            continue
        timings = word_timings[gi] if word_timings and gi < len(word_timings) else None
        n_words = len(text.split('\n')[0].split())
        states = _word_states(start, end, timings, n_words) if timings else None
        if states and len(states) == n_words:
            for wi, (s, e) in enumerate(states):
                png, rect, _ = save(text, wi)
                results.append(dict(start=s, end=e, png=png, rect=rect, word_index=wi))
        else:
            png, rect, _ = save(text, None)
            results.append(dict(start=start, end=end, png=png, rect=rect, word_index=None))
    return results
