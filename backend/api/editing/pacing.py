"""Professional pacing without ML: clean clip bounds, dead-air removal, punch-ins.

Architecture plan section 5, "Dead air and boundaries". Everything here is a
transparent rule over cue/word timings and two cheap ffmpeg audio measurements
(``silencedetect`` from ``analysis.sample_signals`` and ``astats`` RMS).

Inputs
    cues / words: dicts ``{start, end, text, ...}`` in source seconds. A cue may
    carry ``words: [{start, end, text|word}]``; word timings are used when present.
    silence: ``[(start, end), ...]`` from ``sample_signals(...)['silence']``.
    scene boundaries: source seconds, from ``sample_signals(...)['scene_boundaries']``.

Outputs (plain data, converted into the Timeline by a later package)
    keep intervals: ``[(start, end), ...]`` source-time pairs, sorted, disjoint.
    zoom keys: ``[(t, zoom), ...]`` with step (cut) semantics, ``t`` in source seconds.

Pause-preservation rule (humour / drama), documented and deliberately simple:
    A silence is a *protected pause* when the speech before it ends with a
    question mark, or the next sentence is punchline-like: it ends with terminal
    punctuation within at most ``PUNCHLINE_MAX_WORDS`` (4) words. A protected
    pause is shortened to ``dramatic_pause`` (default 0.8 s) instead of being cut
    down to the 2 x 0.12 s padding, and pauses already shorter than that are left
    untouched. This keeps the beat before "...and it worked." or after "Guess
    what happened next?" while still removing long dead air.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from api.services.media import run

TERMINAL = ('.', '!', '?', '।', '…', '॥')
_TRAILING = '"\'”’)]}»'
PUNCHLINE_MAX_WORDS = 4
DEFAULT_PAD = .12
DEFAULT_MIN_GAP = .6
DEFAULT_MAX_REMOVAL = .25
DEFAULT_DRAMATIC_PAUSE = .8

DEFAULT_EMPHASIS = frozenset('''
never always secret huge insane crazy biggest best worst only actually literally million billion
shocking wow important must stop nobody everyone everything nothing free mistake truth real
seriously exactly finally problem warning instantly impossible
sach sabse bahut zaroor kabhi bilkul pakka sirf asli galti dhyan
सच सबसे बहुत ज़रूर जरूर कभी बिल्कुल सिर्फ़ सिर्फ असली गलती
'''.split())


# --------------------------------------------------------------------------- text helpers

def _text(item):
    return str(item.get('text') or item.get('word') or '')


def _ends_sentence(text):
    return text.strip().rstrip(_TRAILING).endswith(TERMINAL)


def _ends_question(text):
    return text.strip().rstrip(_TRAILING).endswith('?')


def _norm_word(word):
    return re.sub(r'[^\wऀ-ॿ]+', '', word.casefold())


def _valid(item):
    try:
        s, e = float(item['start']), float(item['end'])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(s) and math.isfinite(e) and e >= s


def speech_tokens(items):
    """Flatten cues to the finest available timed spans: words when present, else cues."""
    tokens = []
    for item in items or ():
        words = item.get('words') if isinstance(item, dict) else None
        if words:
            tokens.extend((float(w['start']), float(w['end']), _text(w)) for w in words if _valid(w))
        elif isinstance(item, dict) and _valid(item):
            tokens.append((float(item['start']), float(item['end']), _text(item)))
    tokens.sort(key=lambda t: (t[0], t[1]))
    return tokens


def timed_words(items):
    """Word-level (start, end, word) list; cue text without word timings is spread
    linearly by character offset (an approximation, flagged by callers)."""
    out = []
    for item in items or ():
        if item.get('words'):
            out.extend((float(w['start']), float(w['end']), _text(w)) for w in item['words'] if _valid(w))
            continue
        if not _valid(item):
            continue
        s, e, text = float(item['start']), float(item['end']), _text(item)
        total = max(1, len(text))
        for m in re.finditer(r'\S+', text):
            out.append((s + (e - s) * m.start() / total, s + (e - s) * m.end() / total, m.group()))
    out.sort(key=lambda w: w[0])
    return out


# --------------------------------------------------------------------------- bounds

@dataclass
class Bounds:
    start: float
    end: float
    start_reason: str
    end_reason: str

    def as_tuple(self):
        return (self.start, self.end)


def _inside_token(t, tokens, margin=1e-3):
    return any(s + margin < t < e - margin for s, e, _ in tokens)


def snap_bounds(start, end, cues, scene_boundaries=(), duration=None, min_length=1.0,
                start_window=(-2.0, 1.5), end_window=4.0, scene_snap=.5):
    """Move a candidate ``(start, end)`` onto clean edit points.

    Start: the nearest cue start within ``start_window`` of the candidate,
    preferring sentence starts (first cue, or the previous cue ended with
    terminal punctuation; non-sentence starts pay a 1 s penalty). If a scene
    boundary lies up to ``scene_snap`` *before* that cue start and after the
    previous cue's end, the start moves back onto the cut (visually clean, no
    word clipped). Without cues, a nearby scene boundary that is not inside a
    word is used.

    End: the nearest cue end with terminal punctuation within +-``end_window``;
    otherwise the nearest cue end; otherwise unchanged (pulled out of a word if
    possible). A scene boundary up to ``scene_snap`` after the chosen end and
    before the next cue starts extends the end onto the cut.
    """
    cues = sorted((c for c in (cues or ()) if _valid(c)), key=lambda c: float(c['start']))
    tokens = speech_tokens(cues)
    scenes = sorted(float(b) for b in (scene_boundaries or ()))
    limit = float(duration) if duration else math.inf
    new_start, start_reason = float(start), 'unchanged'

    best = None
    for i, c in enumerate(cues):
        cs = float(c['start'])
        if start + start_window[0] <= cs <= start + start_window[1]:
            sentence = i == 0 or _ends_sentence(_text(cues[i - 1]))
            score = abs(cs - start) + (0 if sentence else 1.0)
            if best is None or score < best[0]:
                best = (score, i, sentence)
    if best:
        _, i, sentence = best
        new_start = float(cues[i]['start'])
        start_reason = 'sentence_start' if sentence else 'cue_start'
        prev_end = float(cues[i - 1]['end']) if i else -math.inf
        near = [b for b in scenes if new_start - scene_snap <= b <= new_start and b >= prev_end]
        if near:
            new_start, start_reason = max(near), start_reason + '+scene'
    else:
        near = [b for b in scenes if abs(b - start) <= scene_snap and not _inside_token(b, tokens)]
        if near:
            new_start, start_reason = min(near, key=lambda b: abs(b - start)), 'scene'
    new_start = max(0.0, new_start)

    new_end, end_reason = min(float(end), limit), 'unchanged'
    floor = new_start + min_length
    in_window = [c for c in cues if abs(float(c['end']) - end) <= end_window
                 and floor <= float(c['end']) <= limit]
    punct = [c for c in in_window if _ends_sentence(_text(c))]
    pick = min(punct or in_window, key=lambda c: abs(float(c['end']) - end), default=None)
    if pick is not None:
        new_end, end_reason = float(pick['end']), 'sentence_end' if punct else 'cue_end'
        next_start = min((float(c['start']) for c in cues if float(c['start']) >= new_end - 1e-6), default=math.inf)
        after = [b for b in scenes if new_end <= b <= min(new_end + scene_snap, next_start, limit)]
        if after:
            new_end, end_reason = min(after), end_reason + '+scene'
    else:
        holder = next(((s, e) for s, e, _ in tokens if s < new_end < e), None)
        if holder and holder[1] - new_end <= end_window and holder[1] <= limit:
            new_end, end_reason = holder[1], 'word_end'
    if new_end < floor:
        new_end, end_reason = min(limit, max(floor, float(end))), 'min_length'
    return Bounds(round(new_start, 3), round(new_end, 3), start_reason, end_reason)


# --------------------------------------------------------------------------- dead air

@dataclass
class KeepResult:
    intervals: list                  # [(start, end)] source seconds, kept
    removed: list                    # [(start, end)] source seconds, cut out
    removed_seconds: float = 0.0
    kept_seconds: float = 0.0
    removal_ratio: float = 0.0
    capped: bool = False             # True when the 25 % cap trimmed the removals
    protected: list = field(default_factory=list)   # [(start, end, reason)] pauses kept for timing
    skipped: str | None = None       # reason when no removal was attempted


def _intersect(a, b, spans):
    for s, e in spans:
        lo, hi = max(a, s), min(b, e)
        if hi > lo:
            yield lo, hi


def _protected_reason(a, b, tokens):
    before = [t for t in tokens if t[1] <= a + 1e-6]
    after = [t for t in tokens if t[0] >= b - 1e-6]
    if before and _ends_question(before[-1][2]):
        return 'after_question'
    count = 0
    for _, _, text in after:
        for word in text.split():
            count += 1
            if count > PUNCHLINE_MAX_WORDS:
                return None
            if _ends_sentence(word):
                return 'before_punchline'
    return None


def keep_intervals(start, end, words_or_cues, silence=None, min_gap=DEFAULT_MIN_GAP, pad=DEFAULT_PAD,
                   max_removal=DEFAULT_MAX_REMOVAL, require_transcript=True,
                   dramatic_pause=DEFAULT_DRAMATIC_PAUSE, min_cut=.1):
    """Keep-intervals for ``[start, end)`` with internal dead air removed.

    A region is removable when it is longer than ``min_gap``, no word/cue
    overlaps it, it lies strictly inside the clip (speech on both sides when a
    transcript exists), and, when ``silence`` is given, the audio is silent
    there. ``silence=None`` means "no audio analysis": transcript gaps alone are
    used. ``pad`` seconds of the pause are kept on each side, so a cut never
    touches a word. Total removal is capped at ``max_removal`` of the clip,
    longest pauses first, the last one shortened to fit.
    """
    start, end = float(start), float(end)
    length = max(0.0, end - start)
    whole = KeepResult([(start, end)], [], 0.0, length, 0.0)
    if length <= 0:
        whole.skipped = 'empty_range'
        return whole
    tokens = [t for t in speech_tokens(words_or_cues) if t[1] > start and t[0] < end]
    silence = None if silence is None else [(float(a), float(b)) for a, b in silence if float(b) > float(a)]
    if not tokens and require_transcript:
        whole.skipped = 'no_transcript'
        return whole
    if not tokens and silence is None:
        whole.skipped = 'no_transcript_or_silence'
        return whole

    regions = []
    if tokens:
        reach = tokens[0][1]
        for s, e, _ in tokens[1:]:
            if s > reach:
                gap = (reach, s)
                regions.extend(_intersect(*gap, silence) if silence is not None else [gap])
            reach = max(reach, e)
    else:
        regions = [(max(a, start), min(b, end)) for a, b in silence if a > start and b < end]

    cuts, protected = [], []
    for a, b in regions:
        if b - a <= min_gap:
            continue
        reason = _protected_reason(a, b, tokens) if tokens else None
        if reason:
            protected.append((round(a, 3), round(b, 3), reason))
        retained = max(2 * pad, dramatic_pause) if reason else 2 * pad
        lo, hi = a + pad, b - (retained - pad)
        if hi - lo >= min_cut:
            cuts.append((lo, hi))

    budget, capped, accepted = max_removal * length, False, []
    for lo, hi in sorted(cuts, key=lambda c: c[1] - c[0], reverse=True):
        size = hi - lo
        if size > budget + 1e-9:
            capped = True
            if budget < min_cut:
                continue
            trim = (size - budget) / 2
            lo, hi = lo + trim, hi - trim
            size = hi - lo
        accepted.append((lo, hi))
        budget -= size
    accepted.sort()
    # Defensive invariant: no cut may overlap a word.
    for lo, hi in accepted:
        assert not any(s < hi and e > lo for s, e, _ in tokens), 'cut overlaps speech'

    intervals, cursor = [], start
    for lo, hi in accepted:
        if lo > cursor:
            intervals.append((round(cursor, 3), round(lo, 3)))
        cursor = hi
    if end > cursor:
        intervals.append((round(cursor, 3), round(end, 3)))
    removed = [(round(lo, 3), round(hi, 3)) for lo, hi in accepted]
    removed_s = sum(hi - lo for lo, hi in accepted)
    return KeepResult(intervals, removed, round(removed_s, 3), round(length - removed_s, 3),
                      round(removed_s / length, 4), capped, protected)


def remap(t, intervals):
    """Source time -> output time through keep-intervals (output starts at 0).

    Times inside a removed region map to the cut point; times before the first
    interval map to 0 and after the last to the total kept length. Monotonic
    non-decreasing by construction.
    """
    out = 0.0
    for s, e in intervals:
        if t < s:
            return out
        if t <= e:
            return out + (t - s)
        out += e - s
    return out


def remap_items(items, intervals, min_duration=1e-3):
    """Remap cue or word dicts (and nested ``words``) to output time.

    Returns new dicts; starts are forced non-decreasing and ``end >= start``.
    Items that collapse to nothing (entirely inside removed regions) are dropped.
    """
    out, last = [], 0.0
    for item in sorted((i for i in items or () if _valid(i)), key=lambda i: float(i['start'])):
        s = max(last, remap(float(item['start']), intervals))
        e = max(s, remap(float(item['end']), intervals))
        if e - s < min_duration and float(item['end']) - float(item['start']) >= min_duration:
            continue
        new = dict(item, start=round(s, 3), end=round(e, 3))
        if item.get('words'):
            new['words'] = remap_items(item['words'], intervals, min_duration)
        out.append(new)
        last = s
    return out


# --------------------------------------------------------------------------- punch-ins

def audio_rms_windows(path, start, end, window=.5):
    """RMS level (dBFS) per ``window`` seconds via ``astats`` -> [(t_centre, db)] in source time."""
    rate = 8000
    n = max(1, int(rate * window))
    _, stderr = run(['ffmpeg', '-hide_banner', '-nostdin', '-ss', f'{start:.3f}', '-t', f'{max(.1, end - start):.3f}',
                     '-i', path, '-vn', '-sn', '-af',
                     f'aresample={rate},aformat=channel_layouts=mono,asetnsamples=n={n}:p=0,'
                     'astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.RMS_level',
                     '-f', 'null', '-'])
    out, t = [], None
    for line in stderr.splitlines():
        m = re.search(r'pts_time:([\d.]+)', line)
        if m:
            t = float(m[1])
            continue
        m = re.search(r'lavfi\.astats\.Overall\.RMS_level=(-?[\d.]+|-?inf)', line)
        if m and t is not None:
            v = float(m[1])
            if math.isfinite(v):
                out.append((round(start + t + window / 2, 3), v))
            t = None
    return out


def rms_peaks(windows, rise_db=6.0):
    """Local maxima at least ``rise_db`` above the median window level -> [(t, db)]."""
    if len(windows) < 3:
        return []
    levels = sorted(v for _, v in windows)
    median = levels[len(levels) // 2]
    peaks = []
    for i, (t, v) in enumerate(windows):
        left = windows[i - 1][1] if i else -math.inf
        right = windows[i + 1][1] if i + 1 < len(windows) else -math.inf
        if v >= median + rise_db and v >= left and v > right:
            peaks.append((t, v))
    return peaks


def punch_points(start, end, items, rms=(), lexicon=DEFAULT_EMPHASIS, min_spacing=6.0, zoom=1.1,
                 hold_max=3.0, include_hook=True):
    """Choose punch-in moments: hook start, emphasis words, audio peaks.

    Priority hook > lexicon word > RMS peak; greedy, at most one per
    ``min_spacing`` seconds. Each point zooms in by cut to ``zoom`` and back to
    1.0 at the end of the current sentence (capped at ``hold_max`` s, the next
    punch and the clip end). Returns dicts ``{t, end, zoom, reason, detail}``.
    ``rms`` is ``[(t, db)]`` peaks, e.g. ``rms_peaks(audio_rms_windows(...))``.
    """
    zoom = min(1.25, max(1.0, float(zoom)))
    words = [w for w in timed_words(items) if start <= w[0] < end]
    cands = []
    if include_hook:
        first = next((w[0] for w in words if w[0] <= start + 3), start)
        cands.append((3, 0.0, first, 'hook', ''))
    lex = {_norm_word(w) for w in lexicon}
    for s, _, text in words:
        if _norm_word(text) in lex:
            cands.append((2, 0.0, s, 'emphasis', _norm_word(text)))
    for t, db in rms or ():
        if start <= t < end:
            cands.append((1, db, t, 'audio_peak', f'{db:.1f} dB'))
    chosen = []
    for prio, score, t, reason, detail in sorted(cands, key=lambda c: (-c[0], -c[1], c[2])):
        if t > end - .5:
            continue
        if all(abs(t - c['t']) >= min_spacing for c in chosen):
            chosen.append({'t': round(t, 3), 'zoom': zoom, 'reason': reason, 'detail': detail})
    chosen.sort(key=lambda c: c['t'])
    sentence_ends = [w[1] for w in words if _ends_sentence(w[2])]
    for i, c in enumerate(chosen):
        nxt = chosen[i + 1]['t'] if i + 1 < len(chosen) else end
        back = next((e for e in sentence_ends if e > c['t'] + .4), c['t'] + hold_max)
        c['end'] = round(min(back, c['t'] + hold_max, nxt, end), 3)
    return chosen


def zoom_keys(points, start):
    """Punch points -> step zoom keys [(t, zoom)] in source seconds, starting at 1.0."""
    keys = [(round(start, 3), 1.0)]
    for p in points:
        if p['end'] <= p['t']:
            continue
        if keys[-1][0] == p['t']:
            keys[-1] = (p['t'], p['zoom'])
        else:
            keys.append((p['t'], p['zoom']))
        keys.append((p['end'], 1.0))
    return keys
