"""Explainable heuristic scorers for hook, coherence, payoff, novelty, audio, clarity.

Everything here is a lightweight lexical/signal heuristic. Scores rank candidate moments for
review; they are NOT predictions of virality or viewer behaviour. Each scorer returns
``(value in 0..1, explanation string)`` so the UI/report can show why a number was given.
"""
import math
import re
import unicodedata
from statistics import median

from . import lexicon as lx

SENTENCE_END = re.compile(r'(?<=[.!?।…])\s+')
TERMINAL = ('.', '!', '?', '।', '…')
NUMBER_RE = re.compile(r'\d+(?:[.,]\d+)*')


def clamp(x, lo=0., hi=1.):
    return max(lo, min(hi, x))


def sigmoid(z):
    return 1 / (1 + math.exp(-z))


def sentences(text):
    return [s.strip() for s in SENTENCE_END.split(text.strip()) if s.strip()]


def _digits(text):
    """Digit runs with Devanagari/other Unicode digits mapped to ASCII."""
    out = []
    for run in re.findall(r'[\d०-९]+(?:[.,][\d०-९]+)*', unicodedata.normalize('NFC', text)):
        out.append(''.join(str(unicodedata.digit(c, 0)) if c.isdigit() else c for c in run))
    return out


def _has_number(text):
    return bool(_digits(text))


def _hits(tokens, vocab):
    return [t for t in tokens if t in vocab]


# ---------------------------------------------------------------- hook

# Weighted-sum logistic weights; every active feature is reported in the explanation.
HOOK_BIAS = -1.3
HOOK_WEIGHTS = dict(question=1.6, second_person=.6, number_superlative=.7, contrast=.8, surprise=.5,
                    latency=-.9, filler=-1.1, energy_rise=.5, scene_change=.4)


def hook_features(text, *, latency=None, energy_rise=None, scene_change=None):
    """Hook strength of an opening sentence / first ~3 s. Returns (0..1, explanation).

    ``latency``: seconds before the first word (None = unknown). ``energy_rise``: change in
    normalised loudness over the first 2 s (None = no audio signal). ``scene_change``: bool/None.
    Unavailable signals are skipped, never invented.
    """
    text = (text or '').strip()
    if not text:
        return .2, 'no speech in the opening seconds'
    first = sentences(text)[0]
    toks = lx.tokenize(first)[:30]
    folded = lx.norm(first)
    z, notes = HOOK_BIAS, []

    def add(name, why):
        nonlocal z
        z += HOOK_WEIGHTS[name]
        notes.append(why)

    asks = '?' in first or (bool(toks) and (toks[0] in lx.QUESTION_WORDS_EN or toks[0] in lx.QUESTION_AUX_EN)) \
        or any(t in lx.QUESTION_WORDS_HINGLISH or t in lx.QUESTION_WORDS_HI for t in toks)
    if asks:
        add('question', 'question opener')
    sp = _hits(toks, lx.SECOND_PERSON)
    if sp:
        add('second_person', f"addresses the viewer ('{sp[0]}')")
    sup = _hits(toks, lx.SUPERLATIVES)
    if _has_number(first) or sup:
        add('number_superlative', 'number' if _has_number(first) and not sup else f"superlative/absolute ('{sup[0]}')" if sup else 'number')
    con = _hits(toks, lx.CONTRAST)
    phrase = [p for p in lx.CONTRAST_PHRASES if p in folded]
    if con or phrase:
        add('contrast', f"contrast/stakes marker ('{(phrase or con)[0]}')")
    sur = _hits(toks, lx.SURPRISE)
    if sur or first.endswith('!'):
        add('surprise', f"surprise marker ('{sur[0]}')" if sur else 'exclamation')
    if toks and (toks[0] in lx.FILLERS or any(folded.startswith(p) for p in lx.FILLER_PHRASES)):
        add('filler', f"filler start ('{toks[0]}')")
    if latency is not None and latency > .8:
        scale = clamp((latency - .8) / 2)
        z += HOOK_WEIGHTS['latency'] * scale
        notes.append(f'first word only after {latency:.1f}s')
    if energy_rise is not None and energy_rise > .08:
        z += HOOK_WEIGHTS['energy_rise'] * clamp(energy_rise / .3)
        notes.append('audio energy rises in the first 2s')
    if scene_change:
        add('scene_change', 'scene change in the first 1.5s')
    value = sigmoid(z)
    return value, ('; '.join(notes) if notes else 'plain statement opener, no hook features found')


# ---------------------------------------------------------------- coherence / standalone

def standalone_score(text):
    """Penalise openings that depend on context outside the clip. Returns (0..1, note)."""
    toks = lx.tokenize(text)[:3]
    if not toks:
        return .5, 'no transcript'
    low = lx.norm(text)
    if low.startswith('and then') or low.startswith('aur phir') or low.startswith('और फिर'):
        return .15, "opens with 'and then' (needs earlier context)"
    if toks[0] in lx.UNRESOLVED_OPENERS:
        return .35, f"opens with unresolved '{toks[0]}'"
    if toks[0] in lx.DANGLING_CONJUNCTIONS:
        return .5, f"opens with connective '{toks[0]}'"
    return 1., 'opening is self-contained'


def coherence_score(text, start, first_cue_start, ends_on_cue):
    """Starts on a cue boundary, no dangling conjunction, ends with punctuation, standalone opening."""
    text = (text or '').strip()
    if not text:
        return None
    toks = lx.tokenize(text)
    gap = abs(first_cue_start - start) if first_cue_start is not None else 2.
    boundary = 1. if gap <= .35 else clamp(1 - (gap - .35) / 1.65)
    dangling = 0. if toks and toks[0] in lx.DANGLING_CONJUNCTIONS else 1.
    punct = 1. if text.endswith(TERMINAL) else (.5 if ends_on_cue else .15)
    stand, stand_note = standalone_score(text)
    value = .3 * boundary + .15 * dangling + .25 * punct + .3 * stand
    why = (f"start {'on' if boundary > .8 else 'near' if boundary > .3 else 'away from'} a phrase start; "
           f"{'ends with punctuation' if punct == 1 else 'ends on a phrase boundary' if punct == .5 else 'ends mid-phrase'}; {stand_note}"
           + ('; opens with a dangling conjunction' if not dangling else ''))
    return value, why


# ---------------------------------------------------------------- payoff / humor

def payoff_score(text, energy_last_third_peak=None):
    """Terminal punctuation, resolution/exclamation markers, humor, energy peak in the last third."""
    text = (text or '').strip()
    if not text:
        return None
    sents = sentences(text)
    last = sents[-1]
    tail = ' '.join(sents[-2:])
    toks = lx.tokenize(tail)
    folded = lx.norm(tail)
    parts, notes = [], []
    term = text.endswith(TERMINAL)
    parts.append((.35, 1. if term else .2))
    notes.append('ends on terminal punctuation' if term else 'no terminal punctuation')
    res = _hits(toks, lx.RESOLUTION) or [p for p in lx.RESOLUTION_PHRASES if p in folded]
    bang = last.endswith('!')
    parts.append((.3, 1. if res else (.6 if bang else 0.)))
    notes.append(f"resolution marker ('{res[0]}')" if res else 'exclamatory close' if bang else 'no resolution marker')
    humor = _hits(toks, lx.HUMOR)
    parts.append((.1, 1. if humor else 0.))
    if humor:
        notes.append(f"humor marker ('{humor[0]}')")
    if energy_last_third_peak is not None:
        parts.append((.25, energy_last_third_peak))
        notes.append('loudness peaks in the last third' if energy_last_third_peak > .7 else 'no loudness peak near the end')
    total = sum(w for w, _ in parts)
    return sum(w * v for w, v in parts) / total, '; '.join(notes)


# ---------------------------------------------------------------- audio

def energy_slice(energy, a, b):
    return [v for t, v in energy if a <= t < b]


def energy_rise(energy, start):
    first, second = energy_slice(energy, start, start + 1), energy_slice(energy, start + 1, start + 2)
    if not first or not second:
        return None
    return sum(second) / len(second) - sum(first) / len(first)


def last_third_peak(energy, start, end):
    window = energy_slice(energy, start, end)
    third = energy_slice(energy, start + (end - start) * 2 / 3, end)
    if len(window) < 6 or not third:
        return None
    hi = max(window)
    return clamp(max(third) / hi) if hi > 0 else None


def audio_score(energy, silent_fraction, start, end):
    """Audio excitement from loudness variation (std, dynamic range) blended with 1 - silence."""
    base = max(0., 1 - silent_fraction)
    window = energy_slice(energy or [], start, end)
    if len(window) < 6:
        return base, f'{silent_fraction:.0%} silence (no loudness series available)'
    mean = sum(window) / len(window)
    std = math.sqrt(sum((v - mean) ** 2 for v in window) / len(window))
    rng = max(window) - median(window)
    excite = .55 * clamp(std / .18) + .45 * clamp(rng / .35)
    return .65 * excite + .35 * base, f'loudness variation {std:.2f}, peak over median {rng:.2f}, {silent_fraction:.0%} silence'


# ---------------------------------------------------------------- clarity / information

def clarity_score(text, speech_seconds, window_seconds):
    """Dialogue clarity (pace, speech coverage) and informational value (content words, numbers)."""
    toks = lx.tokenize(text)
    if not toks:
        return None
    wps = len(toks) / max(1., speech_seconds)
    pace = 1. if 1.4 <= wps <= 3.6 else clamp(1 - (1.4 - wps) / 1.4) if wps < 1.4 else clamp(1 - (wps - 3.6) / 3)
    coverage = clamp(speech_seconds / max(1., window_seconds) / .6)
    content = [t for t in toks if t not in lx.STOPWORDS and t not in lx.FILLERS and len(t) > 2]
    info = .5 * clamp(len(content) / len(toks) / .55) + .3 * clamp(len(set(content)) / max(1, len(content)) / .8) \
        + .2 * (1. if _has_number(text) else 0.)
    value = .25 * pace + .2 * coverage + .55 * info
    return value, f'pace {wps:.1f} words/s, speech covers {coverage:.0%} of target, {len(content)} content words ({len(set(content))} distinct)'


# ---------------------------------------------------------------- novelty

def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b and (a | b) else 0.


def build_idf(docs):
    """docs: list of token lists. Smoothed idf per term; None if too few documents to mean anything."""
    if len(docs) < 4:
        return None
    df = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    return {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}, math.log(n + 1) + 1


def distinctiveness(tokens, idf):
    if not idf:
        return None
    table, top = idf
    terms = {t for t in tokens if t not in lx.STOPWORDS and len(t) > 2}
    if not terms:
        return None
    return clamp((sum(table.get(t, top) for t in terms) / len(terms) - 1) / (top - 1))


def novelty_score(tokens, others, idf):
    """1 - max Jaccard vs other top candidates, blended with TF-IDF distinctiveness in the transcript."""
    mine = set(tokens)
    if not mine:
        return None
    sim = max((jaccard(mine, o) for o in others), default=0.)
    dist = distinctiveness(tokens, idf)
    novelty = 1 - sim
    if dist is None:
        return novelty, f'max overlap with other top candidates {sim:.0%}; transcript too short for distinctiveness'
    return .5 * novelty + .5 * dist, f'max overlap with other top candidates {sim:.0%}; term distinctiveness {dist:.2f}'


# ---------------------------------------------------------------- grounded hook overlay text

MAX_OVERLAY_CHARS, MAX_OVERLAY_WORDS = 60, 8


def _prefix(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def grounding_issues(text, transcript):
    """Reasons ``text`` is not supported by ``transcript`` (empty list = grounded).

    Numbers must appear verbatim as digit runs (spoken-form 'five' vs '5' is NOT bridged).
    Non-stopword tokens longer than 3 chars, and ALL-CAPS tokens of any length, must match a
    transcript token exactly or share a >=5-char prefix (inflection tolerance).
    """
    text = unicodedata.normalize('NFC', text or '').strip()
    issues = []
    if not text:
        return ['empty']
    if len(text) > MAX_OVERLAY_CHARS:
        issues.append(f'longer than {MAX_OVERLAY_CHARS} characters')
    source_tokens = set(lx.tokenize(transcript or ''))
    source_numbers = set(_digits(transcript or ''))
    for n in _digits(text):
        if n not in source_numbers:
            issues.append(f'number {n} not in transcript')
    caps = {lx.norm(t) for t in re.findall(r"\b[A-Z]{2,}\b", text)}
    for tok in lx.tokenize(text):
        if tok.isdigit() or all(c.isdigit() for c in tok):
            continue
        needs = (len(tok) > 3 and tok not in lx.STOPWORDS) or tok in caps
        if not needs or tok in source_tokens:
            continue
        if len(tok) >= 5 and any(len(s) >= 5 and _prefix(tok, s) >= 5 for s in source_tokens):
            continue
        issues.append(f"'{tok}' not in transcript")
    return issues


def is_grounded(text, transcript):
    return not grounding_issues(text, transcript)


def hook_overlay(transcript, max_words=MAX_OVERLAY_WORDS, max_chars=MAX_OVERLAY_CHARS):
    """Best extractive span (<=8 words, <=60 chars) from the first two sentences, or None.

    Only words the speaker said are used, so nothing can be fabricated. Returns
    ``{'text', 'source': 'extractive'}``.
    """
    sents = sentences(transcript or '')[:2]
    best = None
    for si, sent in enumerate(sents):
        words = sent.split()
        for i in range(len(words)):
            for j in range(i + 1, min(len(words), i + max_words) + 1):
                span = words[i:j]
                raw = ' '.join(span)
                text = raw.strip(' ,;:-–—"\'')
                if len(span) < 3 and len(words) >= 3 or not text or len(text) > max_chars:
                    continue
                score, _ = hook_features(text)
                last = lx.tokenize(span[-1])[-1:] or ['']
                if last[0] in lx.DANGLING_CONJUNCTIONS or last[0] in lx.STOPWORDS or last[0] in lx.FILLERS:
                    score -= .15
                if i == 0:
                    score += .05
                if j == len(words):
                    score += .05
                    if raw.endswith('?'):
                        score += .05
                score -= .03 * si
                score += .002 * len(span)
                if best is None or score > best[0] + 1e-9:
                    best = (score, text)
    if not best:
        return None
    text = best[1].rstrip('.,;:') if not best[1].endswith('?') else best[1]
    if not text or not is_grounded(text, transcript):
        return None
    return {'text': text, 'source': 'extractive'}
