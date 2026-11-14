"""Transparent heuristics, not predictions of virality or semantic certainty."""
import math
import json
import os
import re
from .media import run, StudioError

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
    return {'visual_samples': frames, 'silence': silence,
            'scene_boundaries': [t for t, score in frames if score > .5]}


def candidates(duration, target, cues, signals, weights=None):
    weights = weights or WEIGHTS
    target = min(target, duration)
    starts = {float(i) for i in range(0, max(1, math.ceil(duration-target)+1), max(2, int(target/3)))}
    starts.update(float(i*target) for i in range(int(duration//target)) if i*target <= duration-target)
    starts.update(c['start'] for c in cues if c['start'] <= duration-target)
    starts = {round(t / 2) * 2 for t in starts if 0 <= t <= duration-target}
    starts.add(max(0, duration-target))
    results = []
    for start in sorted(starts):
        end = min(duration, start+target)
        # Prefer a real phrase end within a few seconds, without invalid boundaries.
        ends = [c['end'] for c in cues if start+target-4 <= c['end'] <= min(duration, start+target+4)]
        if ends:
            end = min(ends, key=lambda x: abs(x-(start+target)))
        text = ' '.join(c['text'] for c in cues if c['end'] > start and c['start'] < end)
        visual = [score for t, score in signals['visual_samples'] if start <= t < end]
        silent = sum(max(0, min(end,b)-max(start,a)) for a,b in signals['silence']) / (end-start)
        speech = bool(text)
        components = dict(hook=.6 if text.endswith('?') or '?' in text[:100] else (.45 if speech else .2),
            coherence=.6 if speech else .25, visual=sum(visual)/max(1,len(visual)),
            audio=max(0, 1-silent), novelty=.5, clarity=.6 if speech else .25,
            payoff=.6 if text.rstrip().endswith(('.', '!', '?')) else .25, feasibility=1.)
        if silent > .85 and not speech:
            components['audio'] = 0
        score = sum(components[k]*weights[k] for k in weights) * 100
        results.append(dict(start=round(start,3), end=round(end,3), score=round(score,1),
            components=components, transcript=text, reason='Bounded scene-change, silence and phrase-boundary heuristics; semantic certainty unavailable.'))
    return results


def select_distinct(items, count=3):
    # Weighted interval scheduling. Greedy ranking can choose a middle clip that
    # blocks three valid non-overlapping outputs, even when enough footage exists.
    import bisect
    ordered = sorted(items, key=lambda c: c['end'])
    ends = [c['end'] for c in ordered]
    dp = [[[] for _ in range(count+1)] for _ in range(len(ordered)+1)]
    def repeated(item, chosen):
        words = set(re.findall(r'\w+', item['transcript'].lower()))
        for prior in chosen:
            other = set(re.findall(r'\w+', prior['transcript'].lower()))
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
