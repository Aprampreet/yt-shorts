"""Candidate scoring (heuristics + optional hosted editorial review) and distinct highlight selection."""
import os

from ..models import GenerationJob
from ..services.analysis import REPLAY_LABEL, candidates, configured_weights, rescore, select_distinct
from ..services.media import StudioError
from .context import update


def score_and_select(ctx):
    job, info, cues, signals, warnings, provider = ctx.job, ctx.info, ctx.cues, ctx.signals, ctx.warnings, ctx.provider
    update(job, 'analyzing', 'Scoring candidate moments', 36)
    target = job.settings.get('target_duration', 30)
    weights = configured_weights()
    pool = candidates(info['duration'], target, cues, signals, weights,
                      heatmap=(ctx.metadata or {}).get('heatmap'))
    if cues and os.getenv('HF_TEXT_MODEL') and os.getenv('HF_TOKEN'):
        for candidate in sorted(pool, key=lambda c: c['score'], reverse=True)[:6]:
            update(job, 'analyzing', 'Reviewing candidate stories with hosted AI', 38)
            if not candidate['transcript']:
                continue
            try:
                editorial = provider.analyze(candidate['transcript'])
                candidate['editorial'] = editorial
                for key in ['hook', 'coherence', 'clarity', 'payoff']:
                    candidate['components'][key] = editorial[key]
                    candidate.setdefault('components_explained', {})[key] = 'hosted editorial review (replaces the local heuristic)'
                rescore(candidate, weights)
                candidate['reason'] = editorial['reason']
            except StudioError as exc:
                warnings.append(exc.message)
                break
    update(job, 'selecting', 'Choosing distinct highlights', 42)
    selected = select_distinct(pool)
    if len(selected) < 3:
        warnings.append(f'Only {len(selected)} non-overlapping highlights fit the source and target duration. No duplicate footage was added.')
    job.analysis = dict(signals=signals, candidates=pool, weights=weights, warnings=warnings,
                        transcription_timing='Phrase subdivisions are approximate; source/provider timestamps retained when available.')
    if any('replay' in c for c in pool):
        job.analysis['replay_signal'] = REPLAY_LABEL
    GenerationJob.objects.filter(pk=job.pk).update(analysis=job.analysis)
    ctx.pool, ctx.selected = pool, selected
