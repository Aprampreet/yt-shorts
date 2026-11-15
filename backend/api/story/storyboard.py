"""Storyboard planning: one scene per script section, timed from the narration word timings.

Visual choice follows the rights hierarchy: the user's owned assets -> commercial_use_permitted -> attribution_required
(attribution recorded) -> original motion-graphic cards (the default; a fully graphics-based video is always possible).
Assets are only ever real MediaAsset rows of the owner; ``SceneSpec`` rejects anything else. ``source_clip`` scenes are
third-party footage: allowed only with a rights declaration, tied to a claim, and capped at 35% of the runtime.
"""
from __future__ import annotations

from django.db import transaction
from pydantic import ValidationError

from ..editing import lexicon as lx
from ..models import MediaAsset, StoryboardScene
from ..services import assets as asset_svc
from ..services.media import StudioError
from . import scoring
from .alignment import approximate_words
from .schemas import SceneSpec

MAX_SOURCE_CLIP_SHARE = 0.35
MIN_SCENE_SECONDS = 0.6
USABLE_LICENSES = ('owned', 'commercial_use_permitted', 'attribution_required')   # auto-selection order
VISUAL_KINDS = ('video', 'image', 'graphic')
FORMAT_LABELS = {'gaming_news': 'Gaming news', 'gameplay_analysis': 'Gameplay analysis', 'hidden_details': 'Hidden details',
                 'game_comparison': 'Comparison', 'explainer': 'Explainer', 'gaming_commentary': 'Commentary',
                 'myth_vs_fact': 'Myth vs fact', 'top_discoveries': 'Top discoveries'}
SOURCE_LABELS = {'source_claim': 'Source: the video', 'inferred': 'Analysis, unconfirmed', 'verified': 'Verified by creator'}


def truncate(text, limit):
    text = ' '.join((text or '').split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if ' ' in cut[limit // 2:]:
        cut = cut[:cut.rfind(' ')]
    return cut.rstrip(' ,;:-') + '…'


def owner_assets(owner):
    return {str(a.pk): a for a in MediaAsset.objects.filter(owner=owner, deleted_at__isnull=True)}


def narration_words(script):
    """(words, timing, duration) for the script's narration asset."""
    asset = script.narration_asset
    if asset is None or asset.deleted_at is not None:
        raise StudioError('narration_required', 'Add narration (synthesized or uploaded) before building the storyboard.')
    duration = asset.duration
    if not duration:
        duration = asset_svc.probe_av(asset_svc.asset_path(asset), 'audio')['duration']
    words = list((asset.metadata or {}).get('words') or [])
    timing = (asset.metadata or {}).get('timing') or 'none'
    if not words:
        words, timing = approximate_words(script.full_text, duration), 'approximate'
    return words, timing, float(duration)


def section_times(script, words, duration):
    """[(name, text, raw_text, start, end)] covering [0, duration]; short sections are merged into the previous."""
    sections = [(name, scoring.strip_markers(raw), raw) for name, raw in scoring.section_list(script.sections or {})]
    sections = [s for s in sections if s[1]]
    if not sections:
        raise StudioError('script_empty', 'The script has no narration text.')
    counts = [len(text.split()) for _, text, _ in sections]
    total, m = sum(counts), len(words)
    starts, k = [], 0
    for c in counts:
        idx = min(m - 1, round(k * m / total)) if m else 0
        starts.append(0.0 if k == 0 or not m else float(words[idx]['start']))
        k += c
    out = []
    for i, (name, text, raw) in enumerate(sections):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(sections) else duration
        if out and (end - start < MIN_SCENE_SECONDS or start <= out[-1][3]):
            prev = out.pop()
            out.append((prev[0], prev[1] + ' ' + text, prev[2] + ' ' + raw, prev[3], end))
            continue
        out.append((name, text, raw, start, end))
    while len(out) > 1 and out[-1][4] - out[-1][3] < MIN_SCENE_SECONDS:
        last = out.pop()
        prev = out.pop()
        out.append((prev[0], prev[1] + ' ' + last[1], prev[2] + ' ' + last[2], prev[3], last[4]))
    name, text, raw, start, _ = out[-1]
    out[-1] = (name, text, raw, start, duration)
    first = out[0]
    out[0] = (first[0], first[1], first[2], 0.0, first[4])
    return out


def _tokens(text):
    return {t for t in lx.tokenize(text) if len(t) > 3 and t not in lx.STOPWORDS}


def _card(name, text, claim, script, fmt, core_labels, index):
    title = (script.sections or {}).get('title') or ''
    sents = scoring.sentences(text)
    first = sents[0] if sents else text
    if name == 'hook':
        return 'title_card', {'kind': 'title_card', 'title': truncate(first, 72),
                              'kicker': truncate(FORMAT_LABELS.get(fmt, 'Commentary'), 32)}
    if name == 'cta':
        return 'title_card', {'kind': 'title_card', 'title': truncate(first, 72), 'kicker': truncate(title, 32) or None}
    if name == 'payoff':
        return 'callout', {'kind': 'callout', 'text': truncate(first, 90)}
    if name.startswith('core') and fmt == 'top_discoveries' and core_labels:
        n = int(name.split('_')[1])
        return 'numbered_list', {'kind': 'numbered_list', 'items': [truncate(x, 72) for x in core_labels[:min(n, 5)]],
                                 'reveal': 'all'}
    if name.startswith('core') and index % 2 == 0:
        return 'callout', {'kind': 'callout', 'text': truncate(first, 90)}
    rest = ' '.join(sents[1:]) or first
    spec = {'kind': 'fact_card', 'headline': truncate(first, 90), 'body': truncate(rest, 240)}
    if claim:
        spec['source_label'] = SOURCE_LABELS.get(claim.get('status'), 'Source: the video')
    return 'fact_card', spec


def validate_scene(data, *, assets, project, claims, duration):
    try:
        return SceneSpec.model_validate(data, context={'assets': assets, 'project': project, 'claims': claims,
                                                       'duration': duration})
    except ValidationError as exc:
        first = exc.errors()[0]
        message = str(first.get('msg', 'invalid scene')).removeprefix('Value error, ')
        raise StudioError('invalid_scene', message) from None


def source_clip_share(specs, duration):
    seconds = sum(s.end - s.start for s in specs if s.visual_type == 'source_clip')
    return seconds / duration if duration else 0.0


def check_share(specs, duration):
    share = source_clip_share(specs, duration)
    if share > MAX_SOURCE_CLIP_SHARE + 1e-6:
        raise StudioError('source_clip_share', f'Third-party source footage would cover {share:.0%} of the video; the '
                          f'limit is {MAX_SOURCE_CLIP_SHARE:.0%}. Use original graphics or your own assets for more scenes.')
    return share


def source_asset(project, assets):
    source = project.source
    if source is None or not source.rights_declaration:
        return None
    for asset in assets.values():
        if asset.origin == 'source_ingest' and asset.source_video_id == source.pk and asset.kind == 'video':
            return asset
    return None


def plan(script, owner, preferred_asset_ids=(), use_source_clips=False):
    """Validated SceneSpecs for the script plus a planning report."""
    project = script.project
    words, timing, duration = narration_words(script)
    assets = owner_assets(owner)
    claims = {c['id']: c for c in (project.intelligence or {}).get('claims') or []}
    fmt = (project.inputs or {}).get('content_format', 'gaming_commentary')
    preferred = []
    for aid in preferred_asset_ids:
        asset = assets.get(str(aid))
        if asset is None:
            raise StudioError('invalid_scene', f'asset {aid} is not one of your library assets')
        if asset.license_status == 'restricted':
            raise StudioError('invalid_scene', f'asset {aid} is restricted and cannot be used in a video')
        if asset.kind not in VISUAL_KINDS or asset.origin == 'source_ingest':
            raise StudioError('invalid_scene', f'asset {aid} is not a usable image or video asset')
        preferred.append(asset)
    auto_pool = sorted((a for a in assets.values() if a.kind in VISUAL_KINDS and a.license_status in USABLE_LICENSES
                        and a.origin not in ('source_ingest', 'generated_render')),
                       key=lambda a: USABLE_LICENSES.index(a.license_status))
    times = section_times(script, words, duration)
    core_labels = [truncate(scoring.sentences(t)[0] if scoring.sentences(t) else t, 72)
                   for n, t, _, _, _ in times if n.startswith('core')]
    src = source_asset(project, assets) if use_source_clips else None
    used, specs, notes, src_seconds = set(), [], [], 0.0
    for i, (name, text, raw, start, end) in enumerate(times):
        cited = scoring.markers(raw)
        claim_id = next((c for c in cited if c in claims), None)
        claim = claims.get(claim_id) if claim_id else None
        data = {'start': round(start, 3), 'end': round(end, 3), 'narration_text': text[:2000], 'claim_id': claim_id,
                'transition': 'cut'}
        asset = None
        if name != 'hook':
            if preferred:
                asset = next((a for a in preferred if str(a.pk) not in used), None)
            else:
                section_toks = _tokens(text)
                asset = next((a for a in auto_pool if str(a.pk) not in used
                              and section_toks & _tokens(a.title + ' ' + ' '.join(a.tags or []))), None)
        length = end - start
        if (asset is None and src is not None and claim and claim.get('status') == 'source_claim'
                and claim.get('start') is not None and name != 'hook'
                and (src_seconds + length) / duration <= MAX_SOURCE_CLIP_SHARE):
            a_in = min(float(claim['start']), max(0.0, (src.duration or 0) - length))
            data.update(visual_type='source_clip', asset_id=str(src.pk), asset_in=round(max(0.0, a_in), 3),
                        asset_out=round(max(0.0, a_in) + length, 3))
            src_seconds += length
            notes.append(f'Scene {i + 1}: third-party source footage tied to claim {claim_id}.')
        elif asset is not None:
            used.add(str(asset.pk))
            if asset.kind == 'video':
                data.update(visual_type='asset_video', asset_id=str(asset.pk), asset_in=0.0, transition='crossfade')
            else:
                data.update(visual_type='asset_image', asset_id=str(asset.pk), transition='crossfade')
        else:
            visual, card = _card(name, text, claim, script, fmt, core_labels, i)
            data.update(visual_type=visual, card=card)
        if i == 0:
            data['transition'] = 'cut'
        specs.append(validate_scene(data, assets=assets, project=project, claims=claims, duration=duration))
    check_share(specs, duration)
    report = {'scenes': len(specs), 'duration': duration, 'word_timing': timing,
              'assets_used': sorted(used), 'source_clip_share': round(source_clip_share(specs, duration), 3),
              'graphics_only': not used and not src_seconds, 'notes': notes}
    if timing == 'approximate':
        report['notes'].append('Scene timing is approximate (no word timestamps for this narration).')
    return specs, report


def scene_fields(spec, assets):
    asset = assets.get(str(spec.asset_id)) if spec.asset_id else None
    attribution = asset.attribution_text if asset is not None and asset.license_status == 'attribution_required' else None
    return dict(start=spec.start, end=spec.end, narration_text=spec.narration_text, visual_type=spec.visual_type,
                asset=asset, asset_in=spec.asset_in, asset_out=spec.asset_out,
                graphic_spec={'card': spec.card, 'claim_id': spec.claim_id, 'attribution': attribution},
                transition=spec.transition, rights_status=asset.license_status if asset is not None else 'generated')


@transaction.atomic
def save(script, specs, owner):
    assets = owner_assets(owner)
    script.scenes.all().delete()
    rows = [StoryboardScene(script=script, order=i + 1, **scene_fields(spec, assets)) for i, spec in enumerate(specs)]
    StoryboardScene.objects.bulk_create(rows)
    return list(script.scenes.select_related('asset').all())


def spec_of(scene):
    spec = scene.graphic_spec or {}
    return {'start': scene.start, 'end': scene.end, 'narration_text': scene.narration_text,
            'visual_type': scene.visual_type, 'asset_id': str(scene.asset_id) if scene.asset_id else None,
            'asset_in': scene.asset_in, 'asset_out': scene.asset_out, 'card': spec.get('card'),
            'claim_id': spec.get('claim_id'), 'transition': scene.transition}


def edit_scene(scene, changes, owner):
    """Apply a SceneEditIn (dict of set fields) with the same validation as generated scenes."""
    script = scene.script
    project = script.project
    data = spec_of(scene)
    for key, value in changes.items():
        if key in ('asset_id', 'claim_id') and value == '':
            value = None
        if key == 'card' and value == {}:
            value = None
        data[key] = value
    if data['visual_type'] not in ('asset_video', 'asset_image', 'source_clip'):
        data['asset_id'] = None
        data['asset_in'] = data['asset_out'] = None
    elif 'card' not in changes and data['visual_type'] in ('asset_video', 'asset_image', 'source_clip'):
        data['card'] = data['card'] if data['card'] and data['card'].get('kind') == 'callout' else None
    assets = owner_assets(owner)
    claims = {c['id']: c for c in (project.intelligence or {}).get('claims') or []}
    duration = script.narration_asset.duration if script.narration_asset else None
    spec = validate_scene(data, assets=assets, project=project, claims=claims, duration=duration)
    if duration:
        other_clip = sum(s.end - s.start for s in script.scenes.exclude(pk=scene.pk) if s.visual_type == 'source_clip')
        own = spec.end - spec.start if spec.visual_type == 'source_clip' else 0.0
        if (other_clip + own) / duration > MAX_SOURCE_CLIP_SHARE + 1e-6:
            raise StudioError('source_clip_share', f'Third-party source footage would cover {(other_clip + own) / duration:.0%} '
                              f'of the video; the limit is {MAX_SOURCE_CLIP_SHARE:.0%}.')
    for field, value in scene_fields(spec, assets).items():
        setattr(scene, field, value)
    scene.save()
    return scene
