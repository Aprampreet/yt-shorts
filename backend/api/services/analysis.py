"""Transparent heuristics, not predictions of virality or semantic certainty."""
import math
import json
import os
import re
from statistics import median
from .media import run, StudioError, sanitize_heatmap
from ..editing import lexicon as lx
from ..editing.hook import (audio_score, build_idf, clamp, clarity_score, coherence_score, energy_rise, hook_features,
                            hook_overlay, last_third_peak, novelty_score, payoff_score, sentences)

WEIGHTS = {'hook': .20, 'coherence': .20, 'visual': .15, 'audio': .10,
           'novelty': .10, 'clarity': .10, 'payoff': .10, 'feasibility': .05}


def configured_weights():
    try:
        weights = json.loads(os.getenv('HIGHLIGHT_WEIGHTS', json.dumps(WEIGHTS)))
        if set(weights) != set(WEIGHTS) or any(not isinstance(v, (int,float)) or not math.isfinite(v) or v < 0 for v in weights.values()):
            raise ValueError()
        total = sum(weights.values())
        if not total:
            raise ValueError()
        return {k:v/total for k,v in weights.items()}
    except (ValueError, TypeError, AttributeError):
        raise StudioError('invalid_weights', 'HIGHLIGHT_WEIGHTS must contain all eight non-negative signal weights and a positive total.')


def sample_signals(path, duration, audio):
    # One 160px frame every two seconds, streaming filters; no full frame extraction.
    _, visual = run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path, '-an', '-vf',
        'fps=1/2,scale=160:-2,select=gte(scene\\,0),metadata=print', '-f', 'null', '-'])
    frames = []
    timestamp = 0
    for line in visual.splitlines():
        match = re.search(r'pts_time:([\d.]+)', line)
        if match:
            timestamp = float(match[1])
        match = re.search(r'lavfi.scene_score=([\d.]+)', line)
        if match:
            frames.append((timestamp, min(1, float(match[1])*5)))
    silence = []
    if audio:
        _, output = run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path, '-vn', '-af',
            'silencedetect=noise=-35dB:d=0.6', '-f', 'null', '-'])
        start = 0
        for line in output.splitlines():
            match = re.search(r'silence_start: ([\d.]+)', line)
            if match:
                start = float(match[1])
            match = re.search(r'silence_end: ([\d.]+)', line)
            if match:
                silence.append((start, float(match[1])))
        if 'silence_start:' in output and (not silence or start > silence[-1][1]):
            silence.append((start, duration))
    energy = []
    if audio:
        # 0.5 s RMS windows -> 0..1 loudness (-60..-10 dB); optional signal, failures are non-fatal.
        try:
            _, out = run(['ffmpeg', '-hide_banner', '-nostdin', '-i', path, '-vn', '-af',
                'aresample=8000,asetnsamples=n=4000:p=0,astats=metadata=1:reset=1,'
                'ametadata=print:key=lavfi.astats.Overall.RMS_level', '-f', 'null', '-'])
            at = 0.
            for line in out.splitlines():
                m = re.search(r'pts_time:([\d.]+)', line)
                if m:
                    at = float(m[1])
                m = re.search(r'RMS_level=(-?[\d.]+|-?inf)', line)
                if m:
                    db = -120. if 'inf' in m[1] else float(m[1])
                    energy.append((round(at, 2), round(min(1., max(0., (db + 60) / 50)), 3)))
        except StudioError:
            energy = []
    return {'visual_samples': frames, 'silence': silence, 'energy': energy,
            'scene_boundaries': [t for t, score in frames if score > .5]}


REPLAY_LABEL = ('YouTube aggregate most-replayed graph (coarse buckets; present for some videos only; '
                'unofficial data via yt-dlp)')
DISCLAIMER = ('Transparent lexical/signal heuristics for ranking candidate moments; not a virality prediction '
              'and not semantic certainty.')


def replay_weight():
    try:
        return clamp(float(os.getenv('REPLAY_WEIGHT', '.15')))
    except ValueError:
        return .15


def _percentile(values, q):
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(len(ordered) - 1, lo + 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def replay_score(start, end, heatmap):
    """Time-weighted mean replay intensity over [start, end], normalised by the video's own
    median and p95, clamped to 0..1. None when there is no usable heatmap (strict no-op)."""
    heatmap = sanitize_heatmap(heatmap)
    if not heatmap or end <= start:
        return None
    values = [b['v'] for b in heatmap]
    med, p95 = median(values), _percentile(values, .95)
    if p95 - med < 1e-9:
        return None  # flat graph carries no information
    covered = weighted = 0.
    for bucket in heatmap:
        overlap = min(end, bucket['e']) - max(start, bucket['s'])
        if overlap > 0:
            covered += overlap
            weighted += overlap * bucket['v']
    if covered <= 0:
        return None
    return clamp(((weighted / covered) - med) / (p95 - med))


def heatmap_peak_starts(heatmap, target, duration, count=5):
    peaks, chosen = sorted(sanitize_heatmap(heatmap), key=lambda b: -b['v']), []
    for bucket in peaks:
        mid = (bucket['s'] + bucket['e']) / 2
        if all(abs(mid - m) >= target / 2 for m in chosen):
            chosen.append(mid)
        if len(chosen) >= count:
            break
    return [min(max(0, m - .35 * target), max(0, duration - target)) for m in chosen]


def _blend(weights, components, replay=None, rw=0.):
    base = sum(components[k] * weights[k] for k in weights) * 100
    if replay is None:
        return base
    return (1 - rw) * base + rw * replay * 100


def rescore(candidate, weights):
    """Recompute a candidate's score after components changed (e.g. hosted editorial override)."""
    info = candidate.get('replay')
    candidate['score'] = round(_blend(weights, candidate['components'], info and info['score'], info['weight'] if info else 0.), 1)
    return candidate['score']


def candidates(duration, target, cues, signals, weights=None, heatmap=None):
    weights = weights or WEIGHTS
    target = min(target, duration)
    heatmap = sanitize_heatmap(heatmap)
    rw = replay_weight() if heatmap else 0.
    starts = {float(i) for i in range(0, max(1, math.ceil(duration-target)+1), max(2, int(target/3)))}
    starts.update(float(i*target) for i in range(int(duration//target)) if i*target <= duration-target)
    starts.update(c['start'] for c in cues if c['start'] <= duration-target)
    starts = {round(t / 2) * 2 for t in starts if 0 <= t <= duration-target}
    starts.update(round(t, 1) for t in heatmap_peak_starts(heatmap, target, duration))
    starts.add(max(0, duration-target))
    energy = signals.get('energy') or []
    visual_samples = signals.get('visual_samples') or []
    scenes = signals.get('scene_boundaries') or []
    idf = build_idf([lx.tokenize(c['text']) for c in cues])
    results = []
    for start in sorted(starts):
        end = min(duration, start+target)
        # Prefer a real phrase end within a few seconds, without invalid boundaries.
        ends = [c['end'] for c in cues if start+target-4 <= c['end'] <= min(duration, start+target+4)]
        if ends:
            end = min(ends, key=lambda x: abs(x-(start+target)))
        span = max(.001, end-start)
        inside = [c for c in cues if c['end'] > start and c['start'] < end]
        text = ' '.join(c['text'] for c in inside)
        visual = [score for t, score in visual_samples if start <= t < end]
        silent = sum(max(0, min(end,b)-max(start,a)) for a,b in signals.get('silence') or []) / span
        speech = bool(text.strip())
        explained, components = {}, {}
        if visual:
            components['visual'] = sum(visual)/len(visual)
            explained['visual'] = f'mean scene-change activity {components["visual"]:.2f} over {len(visual)} samples'
        else:
            components['visual'] = 0.
            explained['visual'] = 'no visual samples in this window'
        components['audio'], explained['audio'] = audio_score(energy, silent, start, end)
        if silent > .85 and not speech:
            components['audio'], explained['audio'] = 0., f'{silent:.0%} silence and no speech'
        completeness = min(1., span / max(.001, target))
        components['feasibility'] = 1. if completeness >= .7 else completeness / .7
        explained['feasibility'] = f'{span:.0f}s window vs {target:.0f}s target'
        entry = dict(start=round(start,3), end=round(end,3), transcript=text, _inside=inside)
        if speech:
            first = sentences(text)[0]
            first_tokens = ' '.join(first.split()[:25])
            latency = max(0., inside[0]['start'] - start)
            rise = energy_rise(energy, start)
            scene = any(start <= t <= start+1.5 for t in scenes) if visual_samples else None
            hook, hook_note = hook_features(first_tokens, latency=latency, energy_rise=rise, scene_change=scene)
            components['hook'], explained['hook'] = hook, hook_note
            ends_on_cue = any(abs(c['end']-end) < .05 for c in inside)
            coh, coh_note = coherence_score(text, start, inside[0]['start'], ends_on_cue)
            components['coherence'], explained['coherence'] = coh, coh_note
            pay, pay_note = payoff_score(text, last_third_peak(energy, start, end))
            components['payoff'], explained['payoff'] = pay, pay_note
            speech_seconds = sum(max(0, min(end, c['end'])-max(start, c['start'])) for c in inside)
            clar, clar_note = clarity_score(text, speech_seconds, span)
            components['clarity'], explained['clarity'] = clar, clar_note
        else:
            for key, value in dict(hook=.2, coherence=.25, clarity=.25, payoff=.25).items():
                components[key], explained[key] = value, 'no transcript: low-confidence floor value'
        entry['_tokens'] = lx.tokenize(text)
        entry['components'], entry['components_explained'] = components, explained
        results.append(entry)
    # Novelty needs the other strong candidates: compare against the top 10 by preliminary score.
    rest = {k: v for k, v in weights.items() if k != 'novelty'}
    norm = sum(rest.values()) or 1.
    def prelim(c):
        return sum(c['components'][k]*w for k, w in rest.items()) / norm
    top = sorted(results, key=prelim, reverse=True)[:10]
    for entry in results:
        if not entry['_tokens']:
            entry['components']['novelty'] = .5
            entry['components_explained']['novelty'] = 'no transcript: neutral value'
            continue
        others = [set(o['_tokens']) for o in top
                  if o is not entry and (o['end'] <= entry['start'] or o['start'] >= entry['end'])]
        entry['components']['novelty'], entry['components_explained']['novelty'] = novelty_score(entry['_tokens'], others, idf)
    reason = 'Bounded scene-change, silence, loudness and phrase-boundary heuristics; semantic certainty unavailable.'
    final = []
    for entry in results:
        entry.pop('_inside'), entry.pop('_tokens')
        components = {k: round(v, 4) for k, v in entry['components'].items()}
        item = dict(entry, components=components, reason=reason, disclaimer=DISCLAIMER)
        item['base_score'] = round(_blend(weights, components), 1)
        if heatmap:
            rs = replay_score(item['start'], item['end'], heatmap)
            if rs is not None:
                item['replay'] = dict(score=round(rs, 4), weight=rw, label=REPLAY_LABEL)
                item['components_explained']['replay'] = f'{REPLAY_LABEL}: {rs:.2f} vs this video\'s median/p95'
        rescore(item, weights)
        if item['transcript'].strip():
            overlay = hook_overlay(item['transcript'])
            if overlay:
                item['hook_overlay'] = overlay
        final.append(item)
    return final


def select_distinct(items, count=3):
    # Weighted interval scheduling. Greedy ranking can choose a middle clip that
    # blocks three valid non-overlapping outputs, even when enough footage exists.
    import bisect
    ordered = sorted(items, key=lambda c: c['end'])
    ends = [c['end'] for c in ordered]
    dp = [[[] for _ in range(count+1)] for _ in range(len(ordered)+1)]
    def repeated(item, chosen):
        words = set(lx.tokenize(item['transcript']))
        for prior in chosen:
            other = set(lx.tokenize(prior['transcript']))
            if words and other and len(words & other)/len(words | other) > .85:
                return True
        return False
    def value(path):
        return len(path), sum(c['score'] for c in path)
    for i, item in enumerate(ordered, 1):
        previous = bisect.bisect_right(ends, item['start'], hi=i-1)
        for k in range(1, count+1):
            best = dp[i-1][k]
            prior = dp[previous][k-1]
            trial = prior + [item] if not repeated(item, prior) else prior
            dp[i][k] = trial if value(trial) > value(best) else best
    return sorted(dp[-1][count], key=lambda c: c['start'])
