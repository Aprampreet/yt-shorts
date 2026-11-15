"""Deterministic Story Studio scoring: originality vs the source, concept diversity, script quality.

Everything here is explainable heuristics. Scores are editorial checks, never predictions of views or virality.
"""
from __future__ import annotations

import itertools
import re
import unicodedata

from ..editing import hook as hook_scorer
from ..editing import lexicon as lx
from .schemas import TARGET_DURATIONS

WPM = {'en': 150, 'hi': 130, 'hinglish': 140}
LENGTH_TOLERANCE = 0.12
THRESHOLD = 0.70
MAX_CONTAINMENT = 0.15
MAX_RUN = 12
SHINGLE = 5
HOOK_MAX_WORDS = 20
READABLE_SENTENCE_WORDS = 18
DUPLICATE_JACCARD = 0.85
MIN_CONCEPT_DISTANCE = 0.35
WEIGHTS = {'structure': .2, 'length_fit': .2, 'grounding': .2, 'originality': .2, 'hook': .1, 'readability': .1}
BASIS = 'Editorial heuristics (structure, length, grounding, originality, hook, readability); not a view or virality prediction.'

MARKER = re.compile(r'\[\s*(c\d{1,3}(?:\s*,\s*c\d{1,3})*)\s*\]', re.I)
SENTENCE = re.compile(r'(?<=[.!?।…])\s+|\n+')
NUMBER = re.compile(r'[0-9०-९]+(?:[.,][0-9०-९]+)*')
DEVANAGARI_DIGITS = str.maketrans('०१२३४५६७८९', '0123456789')

# Attribution ("the source says") and uncertainty markers. Casefolded substrings.
HEDGES = {
    'en': ('according to', 'the video says', 'the source says', 'the creator says', 'the video claims', 'reportedly',
           'reports say', 'claims', 'claimed', 'says', 'said', 'mentions', 'mentioned', 'stated', 'states', 'per the',
           'in the video', 'the source', 'suggests'),
    'hi': ('के अनुसार', 'के मुताबिक', 'के मुताबिक़', 'बताया', 'बताते हैं', 'बताती है', 'कहा', 'कहते हैं', 'कहना है',
           'दावा', 'वीडियो में', 'सोर्स'),
    'hinglish': ('ke mutabik', 'ke hisaab se', 'ke hisab se', 'ke anusaar', 'ke anusar', 'bataya', 'batate hain',
                 'batata hai', 'kehte hain', 'kehta hai', 'kaha', 'claim', 'video mein', 'video me', 'source ke',
                 'according to'),
}
UNCERTAIN = {
    'en': ('might', 'may ', 'could', 'possibly', 'probably', 'likely', 'seems', 'appears', 'perhaps', 'maybe',
           'suggests', 'my guess', 'i think', 'if ', 'unconfirmed', 'speculat'),
    'hi': ('शायद', 'हो सकता', 'हो सकती', 'हो सकते', 'लगता है', 'संभव', 'संभवतः', 'अंदाज़ा', 'अंदाजा', 'अगर'),
    'hinglish': ('shayad', 'ho sakta', 'ho sakti', 'ho sakte', 'lagta hai', 'possible', 'possibly', 'maybe',
                 'might', 'could', 'andaza', 'agar'),
}


# ------------------------------------------------------------------------------------- text helpers

def nfc(text):
    return unicodedata.normalize('NFC', text or '')


def tokens(text):
    """Casefolded NFC word tokens (Latin + Devanagari), punctuation stripped."""
    return lx.tokenize(strip_markers(text))


def strip_markers(text):
    return re.sub(r'\s+([.,!?।])', r'\1', MARKER.sub('', nfc(text))).replace('  ', ' ').strip()


def markers(text):
    found = []
    for group in MARKER.findall(nfc(text)):
        for cid in re.split(r'\s*,\s*', group):
            cid = cid.lower()
            if cid not in found:
                found.append(cid)
    return found


def sentences(text):
    return [s.strip() for s in SENTENCE.split(nfc(text)) if s and s.strip()]


def words(text):
    return strip_markers(text).split()


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def numbers(text):
    return {n.translate(DEVANAGARI_DIGITS).replace(',', '') for n in NUMBER.findall(nfc(text))}


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# ------------------------------------------------------------------------------------- originality

def _shingles(toks, n=SHINGLE):
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def longest_common_run(script_toks, source_toks, n=SHINGLE):
    """Longest run of consecutive tokens shared by script and source, seeded by matching n-grams."""
    if len(script_toks) < n or len(source_toks) < n:
        return 0
    index = {}
    for j in range(len(source_toks) - n + 1):
        index.setdefault(tuple(source_toks[j:j + n]), []).append(j)
    best, i = 0, 0
    while i <= len(script_toks) - n:
        positions = index.get(tuple(script_toks[i:i + n]))
        if not positions:
            i += 1
            continue
        local = n
        for j in positions[:50]:
            k = n
            while i + k < len(script_toks) and j + k < len(source_toks) and script_toks[i + k] == source_toks[j + k]:
                k += 1
            local = max(local, k)
        best = max(best, local)
        i += 1
    return best


def remove_quotes(text, quotes, source_text):
    """Drop explicitly quoted spans that really are verbatim source quotes (attribution is checked by grounding)."""
    norm_source = _flat(source_text)
    excluded = []
    for quote in quotes or []:
        q = (quote or '').strip().strip('"“”\'‘’')
        if len(q.split()) < 3 or len(q.split()) > 25 or _flat(q) not in norm_source:
            continue
        pattern = re.compile(re.escape(q), re.I)
        if pattern.search(text):
            text = pattern.sub(' ', text)
            excluded.append(q)
    return text, excluded


def _flat(text):
    return ' '.join(lx.tokenize(text))


def originality(script_text, source_text, quotes=()):
    text, excluded = remove_quotes(strip_markers(script_text), quotes, source_text)
    script_toks, source_toks = lx.tokenize(text), lx.tokenize(source_text or '')
    sh = _shingles(script_toks)
    containment = (len(sh & _shingles(source_toks)) / len(sh)) if sh else 0.0
    run = longest_common_run(script_toks, source_toks)
    passed = containment <= MAX_CONTAINMENT and run <= MAX_RUN
    return {'containment_5': round(containment, 4), 'longest_run': run, 'passed': passed,
            'excluded_quotes': excluded, 'limits': {'containment_5': MAX_CONTAINMENT, 'longest_run': MAX_RUN},
            'note': 'Cross-script copying (e.g. Devanagari Hindi vs romanised Hinglish) is not detected.'}


# ------------------------------------------------------------------------------------- concept diversity

def concept_tokens(concept):
    text = ' '.join([concept.get('title', ''), concept.get('logline', '')] + list(concept.get('key_points') or []))
    return {t for t in lx.tokenize(text) if len(t) > 2 and t not in lx.STOPWORDS}


def concept_distance(a, b):
    angle = 0.4 if a.get('angle') != b.get('angle') else 0.0
    claims = 0.4 * (1 - jaccard(a.get('claim_ids') or [], b.get('claim_ids') or []))
    content = 0.2 * (1 - jaccard(concept_tokens(a), concept_tokens(b)))
    return round(angle + claims + content, 4)


def concept_quality(concept, known_claims):
    cited = [c for c in concept.get('claim_ids') or [] if c in known_claims]
    invalid = len(concept.get('claim_ids') or []) - len(cited)
    hook_value, _ = hook_scorer.hook_features(concept.get('hook_idea', ''))
    points = len(concept.get('key_points') or [])
    q = (0.35 * min(1.0, len(cited) / 3) + 0.25 * min(1.0, hook_value / 0.55) + 0.2 * (1.0 if 2 <= points <= 5 else 0.5)
         + 0.2 * (1.0 if not invalid else max(0.0, 1 - invalid / max(1, len(concept.get('claim_ids') or [])))))
    return round(q, 4)


def select_diverse(concepts, n, qualities=None):
    """Exhaustive best N-subset maximising mean(quality) + 0.8 * min pairwise distance.

    Returns (indices, min_distance). With n == 1 the distance term is 1.0 (nothing to compare)."""
    qualities = qualities if qualities is not None else [c.get('quality_score', 0.5) for c in concepts]
    n = min(n, len(concepts))
    if n <= 0:
        return [], 0.0
    dist = {}
    for i, j in itertools.combinations(range(len(concepts)), 2):
        dist[(i, j)] = concept_distance(concepts[i], concepts[j])
    best, best_value, best_min = None, -1.0, 0.0
    for combo in itertools.combinations(range(len(concepts)), n):
        pair_d = [dist[(i, j)] for i, j in itertools.combinations(combo, 2)]
        min_d = min(pair_d) if pair_d else 1.0
        value = sum(qualities[i] for i in combo) / n + 0.8 * min_d
        if value > best_value + 1e-9:
            best, best_value, best_min = list(combo), value, min_d
    return best, round(best_min, 4)


# ------------------------------------------------------------------------------------- script quality

def section_list(sections):
    """[(name, text)] in narration order."""
    out = [('hook', sections.get('hook', '')), ('context', sections.get('context', ''))]
    out += [(f'core_{i + 1}', text) for i, text in enumerate(sections.get('core') or [])]
    out.append(('payoff', sections.get('payoff', '')))
    if (sections.get('cta') or '').strip():
        out.append(('cta', sections['cta']))
    return out


def full_text(sections):
    return '\n'.join(strip_markers(t) for _, t in section_list(sections) if strip_markers(t))


def target_window(target_duration):
    return TARGET_DURATIONS.get(target_duration, (30, 45))


def target_words(language, target_duration):
    lo, hi = target_window(target_duration)
    wpm = WPM.get(language, 150)
    return round(lo * wpm / 60), round(hi * wpm / 60)


def _contains_any(text, phrases):
    folded = ' ' + lx.norm(text) + ' '
    return any(p in folded for p in phrases)


def grounding(sections, claims, source_text, language):
    """Cited claim ids must exist; source claims need attribution, inferred claims need uncertainty; every number in
    the script must appear in the source knowledge."""
    claims = claims or {}
    issues, cited = [], []
    hedges = HEDGES.get(language, ()) + HEDGES['en']
    unsure = UNCERTAIN.get(language, ()) + UNCERTAIN['en']
    for _, text in section_list(sections):
        for sentence in sentences(text):
            ids = markers(sentence)
            for cid in ids:
                if cid not in cited:
                    cited.append(cid)
                claim = claims.get(cid)
                if claim is None:
                    issues.append(f'cites unknown claim {cid}')
                    continue
                status = claim.get('status')
                if status == 'source_claim' and not _contains_any(sentence, hedges):
                    issues.append(f'{cid} is a source claim: attribute it ("according to the video…") in "{strip_markers(sentence)[:80]}"')
                elif status == 'inferred' and not _contains_any(sentence, unsure):
                    issues.append(f'{cid} is an inference: do not state it as fact in "{strip_markers(sentence)[:80]}"')
    knowledge = (source_text or '') + ' ' + ' '.join(c.get('text', '') + ' ' + c.get('quote', '') for c in claims.values())
    allowed = numbers(knowledge)
    invented = sorted(n for n in numbers(full_text(sections)) if n not in allowed)
    for n in invented:
        issues.append(f'number {n} does not appear in the source knowledge')
    if claims and not cited:
        issues.append('no claim is cited; mark sentences that use source facts with [c#]')
    score = clamp(1 - 0.2 * len(issues))
    return score, issues, cited, invented


def score_script(sections, *, language, target_duration, claims=None, source_text='', quotes=()):
    """Full quality report for one script. ``passed`` needs the 0.70 threshold and the hard gates (structure
    complete, originality limits, length within ~half of the window)."""
    sections = dict(sections or {})
    text = full_text(sections)
    word_count = len(text.split())
    wpm = WPM.get(language, 150)
    estimated = round(word_count / wpm * 60, 1)
    reasons, explain = [], {}

    # structure
    checks = {name: bool(strip_markers(sections.get(name, '') or '')) for name in ('hook', 'context', 'payoff')}
    checks['core'] = any(strip_markers(c) for c in sections.get('core') or [])
    hook_words = len(words(sections.get('hook', '')))
    checks['hook_short'] = 0 < hook_words <= HOOK_MAX_WORDS
    structure = sum(checks.values()) / len(checks)
    structure_ok = all(v for k, v in checks.items() if k != 'hook_short')
    for k, ok in checks.items():
        if not ok:
            reasons.append(f'hook has {hook_words} words; keep it within {HOOK_MAX_WORDS}' if k == 'hook_short'
                           else f'missing section: {k}')
    explain['structure'] = ', '.join(f'{k} {"ok" if v else "missing"}' for k, v in checks.items())

    # length
    lo, hi = target_window(target_duration)
    lo_ok, hi_ok = lo * (1 - LENGTH_TOLERANCE), hi * (1 + LENGTH_TOLERANCE)
    if lo_ok <= estimated <= hi_ok:
        length_fit = 1.0
    else:
        bound = lo_ok if estimated < lo_ok else hi_ok
        length_fit = clamp(1 - abs(estimated - bound) / (0.5 * bound))
        w_lo, w_hi = target_words(language, target_duration)
        reasons.append(f'length is {word_count} words (~{estimated}s at {wpm} wpm); aim for {w_lo}-{w_hi} words')
    explain['length_fit'] = f'{word_count} words ≈ {estimated}s at {wpm} wpm; target {lo}-{hi}s (±{int(LENGTH_TOLERANCE * 100)}%)'

    # grounding
    ground, issues, cited, invented = grounding(sections, claims, source_text, language)
    reasons += issues
    explain['grounding'] = 'all cited claims grounded and hedged' if not issues else f'{len(issues)} grounding issue(s)'

    # originality
    orig = originality('\n'.join(t for _, t in section_list(sections)), source_text, quotes) \
        if source_text else {'containment_5': 0.0, 'longest_run': 0, 'passed': True, 'excluded_quotes': [],
                             'limits': {'containment_5': MAX_CONTAINMENT, 'longest_run': MAX_RUN},
                             'note': 'No source text to compare against.'}
    if orig['passed']:
        originality_score = 1.0
    else:
        originality_score = min(0.4, clamp(1 - orig['containment_5'] * 2) * (1.0 if orig['longest_run'] <= MAX_RUN else 0.6))
        reasons.append(f'too close to the source (5-gram containment {orig["containment_5"]:.2f}, longest shared run '
                       f'{orig["longest_run"]} words): rewrite in your own words and structure')
    explain['originality'] = f'5-gram containment {orig["containment_5"]:.2f} (≤{MAX_CONTAINMENT}), longest run {orig["longest_run"]} (≤{MAX_RUN})'

    # hook
    hook_value, hook_note = hook_scorer.hook_features(strip_markers(sections.get('hook', '')))
    hook = clamp(hook_value / 0.55)
    if hook < 0.6:
        reasons.append('hook is a plain statement; open with a question, contrast or a concrete stake from the source')
    explain['hook'] = hook_note

    # readability
    sents = [s for _, t in section_list(sections) for s in sentences(strip_markers(t))]
    mean_len = (sum(len(s.split()) for s in sents) / len(sents)) if sents else 0
    readability = 1.0 if mean_len <= READABLE_SENTENCE_WORDS else clamp(1 - (mean_len - READABLE_SENTENCE_WORDS) / READABLE_SENTENCE_WORDS)
    if readability < 1:
        reasons.append(f'sentences average {mean_len:.0f} words; keep them under {READABLE_SENTENCE_WORDS} for narration')
    explain['readability'] = f'mean sentence length {mean_len:.1f} words'

    components = {'structure': round(structure, 3), 'length_fit': round(length_fit, 3), 'grounding': round(ground, 3),
                  'originality': round(originality_score, 3), 'hook': round(hook, 3), 'readability': round(readability, 3)}
    score = round(sum(WEIGHTS[k] * v for k, v in components.items()), 3)
    gates = {'structure_complete': structure_ok, 'originality': orig['passed'], 'length': length_fit >= 0.5}
    passed = score >= THRESHOLD and all(gates.values())
    return {'score': score, 'threshold': THRESHOLD, 'passed': passed, 'gates': gates, 'components': components,
            'explanations': explain, 'reasons': reasons, 'word_count': word_count, 'estimated_duration': estimated,
            'wpm': wpm, 'language': language, 'target_seconds': [lo, hi], 'cited_claim_ids': cited,
            'unsupported_numbers': invented, 'originality': orig, 'basis': BASIS}


# ------------------------------------------------------------------------------------- duplicate guard

def near_duplicates(texts):
    """[(i, j, jaccard)] for narration texts whose token Jaccard exceeds 0.85."""
    toks = [set(tokens(t)) for t in texts]
    out = []
    for i, j in itertools.combinations(range(len(texts)), 2):
        value = jaccard(toks[i], toks[j])
        if value > DUPLICATE_JACCARD:
            out.append((i, j, round(value, 3)))
    return out
