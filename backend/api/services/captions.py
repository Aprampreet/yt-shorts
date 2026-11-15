import html
import re
import json
import os
import platform
import shutil
import subprocess
from .languages import caption_text
from .media import StudioError

# (font, size, primary colour, bold, outline, base MarginV at PlayRes 1080x1920)
STYLES = {
    'gaming': ('DejaVu Sans', 78, '&H0000DDFF', 1, 5, 240),
    'minimal': ('DejaVu Sans', 64, '&H00FFFFFF', 0, 2, 280),
    'cinematic': ('DejaVu Serif', 58, '&H00E8E8E8', 0, 2, 320),
    'dynamic': ('DejaVu Sans', 76, '&H0066FFBB', 1, 4, 260),
    'bilingual': ('DejaVu Sans', 64, '&H00FFFFFF', 1, 4, 300),
}
# The five caption presets (graphics.PRESETS) onto the ASS styles above.
PRESET_STYLES = {'bold_gaming': 'gaming', 'clean_commentary': 'minimal', 'cinematic': 'cinematic',
                 'dynamic_creator': 'dynamic', 'bilingual': 'bilingual'}
# Karaoke highlight colour per style (ASS &HAABBGGRR), matching graphics.PRESETS highlights.
HIGHLIGHTS = {'gaming': '&H00FFFFFF', 'minimal': '&H000AD6FF', 'cinematic': '&H0082C4F0',
              'dynamic': '&H0066FFBB', 'bilingual': '&H000AD6FF'}
SECONDARY_SCALE = .7          # translated line size relative to the original line
PLAY_W, PLAY_H = 1080, 1920



def seconds(value):
    parts = value.replace(',', '.').split(':')
    return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def parse_vtt(path):
    content = path.read_text(errors='replace')
    cues = []
    for block in re.split(r'\n\s*\n', content):
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            if '-->' in line:
                try:
                    a, b = line.split('-->')
                    text = html.unescape(re.sub(r'<[^>]*>', '', ' '.join(lines[i+1:])))
                    start, end = seconds(a.strip()), seconds(b.strip().split()[0])
                    if text and end > start:
                        cues.append(dict(start=start, end=end, text=text, timing='source_subtitle'))
                except ValueError:
                    pass
                break
    # YouTube automatic captions often repeat a rolling phrase. Merge adjacent
    # repeated or expanding cues instead of burning duplicate words repeatedly.
    merged = []
    for cue in cues:
        if merged and cue['start'] <= merged[-1]['end'] + .2:
            previous = merged[-1]
            if cue['text'] == previous['text']:
                previous['end'] = max(previous['end'], cue['end'])
                continue
            if cue['text'].startswith(previous['text']) and len(previous['text']) > 2:
                previous['text'] = cue['text']
                previous['end'] = max(previous['end'], cue['end'])
                continue
            if previous['text'].startswith(cue['text']):
                previous['end'] = max(previous['end'], cue['end'])
                continue
        merged.append(cue)
    return merged


def local_cues(cues, start, end):
    return [dict(c, start=max(c['start'], start)-start, end=min(c['end'], end)-start)
            for c in cues if c['end'] > start and c['start'] < end]


def stamp(value, ass=False):
    value = max(0, value)
    if ass:
        ticks = round(value * 100)
        return f'{ticks//360000}:{ticks//6000%60:02}:{ticks//100%60:02}.{ticks%100:02}'
    ticks = round(value * 1000)
    return f'{ticks//3600000:02}:{ticks//60000%60:02}:{ticks//1000%60:02},{ticks%1000:03}'


def style_name(style):
    """Accept a legacy ``caption_style`` or one of the five caption presets; return the ASS style key."""
    style = PRESET_STYLES.get(style, style)
    return style if style in STYLES else 'minimal'


def _word_items(cue):
    """Real per-word timings carried by the cue (``words``), or None. Never invented."""
    words = cue.get('words')
    if not words:
        return None
    out = []
    for w in words:
        try:
            a, b = float(w['start']), float(w['end'])
        except (KeyError, TypeError, ValueError):
            return None
        text = str(w.get('text') or w.get('word') or '').strip()
        if text and b >= a:
            out.append((a, b, text))
    return out or None


def caption_groups(cues, style='minimal', mode='original'):
    """Split cues into on-screen phrase groups.

    Returns dicts ``{start, end, text, words, timing}``. ``text`` keeps a ``\n`` between the
    original and translated line in bilingual mode (the two languages are stacked, never
    merged into one wrapped line). ``words`` is a list of ``(start, end, word)`` only when the
    cue carried real word timings (``timing='word'``); otherwise phrase boundaries are
    interpolated across the cue and ``timing='approximate'``.
    """
    key = style_name(style)
    step = 5 if key in ('gaming', 'dynamic') else 6 if key == 'bilingual' else 8
    groups = []
    for cue in cues:
        shown = caption_text(cue, mode)
        if not shown:
            continue
        lines = [line.strip() for line in shown.split('\n') if line.strip()]
        primary = lines[0].split()
        secondary = ' '.join(lines[1:]).split() if len(lines) > 1 else []
        if not primary:
            continue
        timed = _word_items(cue) if mode == 'original' and not secondary else None
        if timed and len(timed) == len(primary):
            for offset in range(0, len(timed), step):
                part = timed[offset:offset+step]
                groups.append(dict(start=part[0][0], end=max(part[-1][1], part[0][0] + .05),
                                   text=' '.join(w[2] for w in part), words=part, timing='word'))
            continue
        n1, n2 = len(primary), len(secondary)
        start, length = cue['start'], cue['end'] - cue['start']
        for offset in range(0, n1, step):
            stop = min(offset+step, n1)
            text = ' '.join(primary[offset:stop])
            if secondary:
                lo, hi = round(offset * n2 / n1), round(stop * n2 / n1)
                translated = ' '.join(secondary[lo:hi])
                if translated:
                    text += '\n' + translated
            groups.append(dict(start=start + length * offset / n1, end=start + length * stop / n1, text=text,
                               words=None, timing='approximate'))
    return groups


def _wrap(text, limit, max_lines):
    lines, line = [], ''
    for word in text.split():
        proposal = f'{line} {word}'.strip()
        if len(proposal) > limit and line:
            lines.append(line)
            line = word
        else:
            line = proposal
    if line:
        lines.append(line)
    return lines[:max_lines]


def _ass_clean(text):
    return text.replace('\\', '').replace('{', '').replace('}', '')


def ass_margins():
    """MarginL, MarginR, MarginV at PlayRes 1080x1920 clear of the Shorts/Reels UI (graphics.safe_rect)."""
    from ..editing.graphics import safe_rect
    left, _top, right, bottom = safe_rect((PLAY_W, PLAY_H))
    return max(110, left), max(160, PLAY_W - right), PLAY_H - bottom


def write_srt(groups, path):
    path.write_text('\n\n'.join(f'{i+1}\n{stamp(g["start"])} --> {stamp(g["end"])}\n{g["text"]}'
                                for i, g in enumerate(groups)))
    return path


def write_captions(cues, folder, number, style='minimal', mode='original'):
    """Write ``<number>.srt``, ``<number>.ass`` and ``<number>.json`` for ``cues``.

    ``style`` is a legacy caption_style or a caption preset. Phrase subdivisions interpolate
    timestamps and are explicitly approximate unless cues carry real word timings, in which
    case groups follow the words and the ASS uses per-word karaoke highlighting.
    """
    groups = caption_groups(cues, style, mode)
    key = style_name(style)
    srt = folder / f'{number}.srt'
    ass = folder / f'{number}.ass'
    write_srt(groups, srt)
    font, size, color, bold, outline, margin = STYLES[key]
    if any('ऀ' <= char <= 'ॿ' for g in groups for char in g['text']):
        font = os.getenv('CAPTION_FONT_DEVANAGARI',
            'Arial Unicode MS' if platform.system() == 'Darwin' else 'Noto Sans Devanagari')
        if not font.replace(' ','').isalpha():
            raise StudioError('font_config', 'Invalid Devanagari font configuration.')
        if shutil.which('fc-match'):
            resolved = subprocess.run(['fc-match',font,'-f','%{family}'],capture_output=True,text=True,timeout=5)
            if font.lower() not in resolved.stdout.lower():
                raise StudioError('font_missing', f'Devanagari font {font} is missing; install a licensed font before rendering.')
    json_path = folder / f'{number}.json'
    json_path.write_text(json.dumps(cues,ensure_ascii=False,indent=2))
    left, right, safe_v = ass_margins()
    margin_v = max(margin, safe_v)
    karaoke = any(g['words'] for g in groups)
    highlight = HIGHLIGHTS[key]
    styles = [f'Style: Main,{font},{size},{color},&H00FFFFFF,&H00101010,&H90000000,{bold},0,0,0,100,100,0,0,1,'
              f'{outline},2,2,{left},{right},{margin_v},1']
    if karaoke:
        base = color if color != highlight else '&H00FFFFFF'
        styles.append(f'Style: Karaoke,{font},{size},{highlight},{base},&H00101010,&H90000000,{bold},0,0,0,100,100,0,0,1,'
                      f'{outline},2,2,{left},{right},{margin_v},1')
    header = '\n'.join([
        '[Script Info]', 'ScriptType: v4.00+', f'PlayResX: {PLAY_W}', f'PlayResY: {PLAY_H}', 'WrapStyle: 0',
        '[V4+ Styles]',
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, '
        'Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, '
        'MarginR, MarginV, Encoding',
        *styles, '[Events]', 'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text', ''])
    effect = r'{\fad(90,90)}'
    if key in ('gaming', 'bilingual'):
        effect = r'{\fscx96\fscy96\t(0,120,\fscx100\fscy100)\fad(60,60)}'
    elif key == 'dynamic':
        effect = r'{\fad(70,70)\fscx98\fscy98\t(0,100,\fscx100\fscy100)}'
    elif key == 'cinematic':
        effect = r'{\fad(180,180)}'
    events = []
    for g in groups:
        a, b = g['start'], g['end']
        if g['words']:
            # Karaoke: each word switches from the base to the highlight colour at its real start time.
            words, rows, row, used = g['words'], [], [], ''
            for i, (ws, _we, word) in enumerate(words):
                nxt = words[i+1][0] if i + 1 < len(words) else b
                cs = max(1, round((nxt - ws) * 100))
                lead = max(0, round((ws - a) * 100)) if i == 0 else 0
                token = (f'{{\\k{lead}}}' if lead else '') + f'{{\\k{cs}}}{_ass_clean(word)}'
                proposal = f'{used} {word}'.strip()
                if len(proposal) > 25 and row:
                    rows.append(' '.join(row))
                    row, used = [token], word
                else:
                    row.append(token)
                    used = proposal
            if row:
                rows.append(' '.join(row))
            events.append(f'Dialogue: 0,{stamp(a, True)},{stamp(b, True)},Karaoke,,0,0,0,,{effect}' + '\\N'.join(rows[:3]))
            continue
        paragraphs = [p for p in _ass_clean(g['text']).split('\n') if p.strip()]
        if len(paragraphs) >= 2:
            # Bilingual: original line(s) at full size, translated line(s) stacked below at 70 % size.
            first = _wrap(paragraphs[0], 25, 2)
            second = _wrap(' '.join(paragraphs[1:]), 34, 2)
            small = round(size * SECONDARY_SCALE)
            text = '\\N'.join(first) + '\\N' + f'{{\\fs{small}\\b0\\c&H00DCD6D6&}}' + '\\N'.join(second)
        else:
            text = '\\N'.join(_wrap(paragraphs[0] if paragraphs else '', 25, 3))
        events.append(f'Dialogue: 0,{stamp(a, True)},{stamp(b, True)},Main,,0,0,0,,{effect}{text}')
    ass.write_text(header + '\n'.join(events))
    return srt, ass
