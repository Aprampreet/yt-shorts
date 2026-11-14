import html
import re
import json
import os
import platform
import shutil
import subprocess
from .languages import caption_text
from .media import StudioError

STYLES = {
    'gaming': ('DejaVu Sans', 78, '&H0000DDFF', 1, 5, 240),
    'minimal': ('DejaVu Sans', 64, '&H00FFFFFF', 0, 2, 280),
    'cinematic': ('DejaVu Serif', 58, '&H00E8E8E8', 0, 2, 320),
    'dynamic': ('DejaVu Sans', 76, '&H0066FFBB', 1, 4, 260),
}


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


def write_captions(cues, folder, number, style='minimal', mode='original'):
    """Phrase subdivisions interpolate timestamps and are explicitly approximate."""
    groups = []
    for cue in cues:
        shown = caption_text(cue, mode)
        if not shown:
            continue
        words = shown.split()
        step = 5 if style in ('gaming', 'dynamic') else 8
        for offset in range(0, len(words), step):
            part = words[offset:offset+step]
            a = cue['start'] + (cue['end']-cue['start']) * offset / len(words)
            b = cue['start'] + (cue['end']-cue['start']) * min(offset+step, len(words)) / len(words)
            groups.append((a, b, ' '.join(part)))
    srt = folder / f'{number}.srt'
    ass = folder / f'{number}.ass'
    srt.write_text('\n\n'.join(f'{i+1}\n{stamp(a)} --> {stamp(b)}\n{text}' for i, (a,b,text) in enumerate(groups)))
    font, size, color, bold, outline, margin = STYLES[style]
    if any('\u0900' <= char <= '\u097f' for _,_,value in groups for char in value):
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
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{size},{color},&H00FFFFFF,&H00101010,&H90000000,{bold},0,0,0,100,100,0,0,1,{outline},2,2,110,160,{margin},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    for a, b, text in groups:
        safe = text.replace('\\', '').replace('{', '').replace('}', '')
        lines = []
        for paragraph in safe.split('\n'):
            line = ''
            for word in paragraph.split():
                proposal = f'{line} {word}'.strip()
                if len(proposal) > 25 and line:
                    lines.append(line)
                    line = word
                else:
                    line = proposal
            if line:
                lines.append(line)
        safe = '\\N'.join(lines[:3])
        effect = r'{\fad(90,90)}'
        if style == 'gaming':
            effect = r'{\fscx96\fscy96\t(0,120,\fscx100\fscy100)\fad(60,60)}'
        elif style == 'dynamic':
            effect = r'{\fad(70,70)\fscx98\fscy98\t(0,100,\fscx100\fscy100)}'
        elif style == 'cinematic':
            effect = r'{\fad(180,180)}'
        events.append(f'Dialogue: 0,{stamp(a, True)},{stamp(b, True)},Main,,0,0,0,,{effect}{safe}')
    ass.write_text(header + '\n'.join(events))
    return srt, ass
