"""Per-platform publishing metadata and the shared rights gate.

Everything here derives only from the final video's transcript/script and its stored title/description.
The optional Groq generator is used when available; otherwise a deterministic extractive fallback applies.
Attribution is appended after description finalization (services.groq.finalize_description strips Source lines).
"""
import re
from collections import Counter

INSTAGRAM_CAPTION_MAX = 2200
INSTAGRAM_MAX_HASHTAGS = 30
YOUTUBE_TITLE_MAX = 100
YOUTUBE_MAX_HASHTAGS = 5
QUESTION_MAX = 200
BLOCKING_LICENSES = frozenset({'unknown', 'permission_pending', 'restricted'})
HASHTAG_RE = re.compile(r'#[\wऀ-ॿ]{1,60}')
_STOPWORDS = frozenset('about after again also because before being between could does doing from have into just like make more most '
                       'much only other over really same should some still such than that their them then there these they '
                       'this those through very want what when where which while will with would your'.split())


class MetadataError(ValueError):
    pass


def normalize_hashtags(tags, limit):
    result = []
    for tag in tags or []:
        tag = str(tag).strip()
        if not tag:
            continue
        tag = '#' + tag.lstrip('#')
        if not HASHTAG_RE.fullmatch(tag):
            raise MetadataError(f'"{tag}" is not a valid hashtag (letters, digits and underscores only).')
        if tag.lower() not in {t.lower() for t in result}:
            result.append(tag)
    if len(result) > limit:
        raise MetadataError(f'At most {limit} hashtags are allowed for this platform.')
    return result


# ---- rights gate -----------------------------------------------------------------------------------------------

def used_assets(short):
    """MediaAssets used by a video via its script's storyboard scenes and narration."""
    script = getattr(short, 'script', None) if short.script_id else None
    if not script:
        return []
    assets = {}
    for scene in script.scenes.select_related('asset').all():
        if scene.asset_id:
            assets[scene.asset_id] = scene.asset
    if script.narration_asset_id:
        assets[script.narration_asset_id] = script.narration_asset
    return list(assets.values())


def attributions_for(short):
    seen = []
    for asset in used_assets(short):
        if asset.license_status == 'attribution_required' and asset.attribution_text.strip():
            text = asset.attribution_text.strip()
            if text not in seen:
                seen.append(text)
    return seen


def rights_blocker(short, basis):
    """Return None when publishing may proceed, else (code, message). Same rule as YouTube plus asset licences."""
    if not short:
        return 'rights_unresolved', 'The source rights declaration is unavailable.'
    source = short.job.source if short.job_id else None
    declared = short.rights_review_status == 'rights_documented' or (
        basis == 'source_declaration' and source is not None and source.rights_declaration)
    if not declared:
        return 'rights_unresolved', 'Complete and document the final rights review before approving publication.'
    for asset in used_assets(short):
        label = asset.title or str(asset.id)
        if asset.license_status in BLOCKING_LICENSES:
            return 'asset_license_blocked', (f'Asset "{label}" has licence status "{asset.license_status}". '
                                             'Resolve its licence in the Media Library before publishing.')
        if asset.license_status == 'attribution_required' and not asset.attribution_text.strip():
            return 'asset_attribution_missing', f'Asset "{label}" requires attribution but has no attribution text.'
    return None


# ---- text helpers ----------------------------------------------------------------------------------------------

def transcript_for(short):
    text = (short.transcript or '').strip()
    if not text and short.script_id and short.script:
        text = (short.script.full_text or '').strip()
    return text


def clean_body(text):
    """Drop source credits, hashtag-only lines and the YouTube-specific call to action."""
    from ..services.groq import CALL_TO_ACTION
    text = (text or '').replace(CALL_TO_ACTION, '')
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r'(?i)^source(?:\s+video)?\s*:', stripped):
            continue
        if stripped and all(HASHTAG_RE.fullmatch(part) for part in stripped.split()):
            continue
        if re.search(r'(?i)\bsubscribe\b', stripped) and re.search(r'(?i)\bcomments?\b', stripped):
            continue
        lines.append(line.rstrip())
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


def _sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?।])\s+', re.sub(r'\s+', ' ', text)) if s.strip()]


def extractive_title(text):
    sentences = _sentences(text)
    first = sentences[0] if sentences else ''
    first = first.rstrip('.!?। ')
    if len(first) > 90:
        first = first[:89].rsplit(' ', 1)[0].rstrip(',;:- ') + '…'
    return first


def extractive_description(text, limit=300):
    out = ''
    for sentence in _sentences(text)[:3]:
        if len(out) + len(sentence) + 1 > limit:
            break
        out = (out + ' ' + sentence).strip()
    return out or text[:limit].strip()


def keyword_hashtags(text, count=3):
    words = re.findall(r'[A-Za-z]{5,20}', text.lower())
    ranked = Counter(w for w in words if w not in _STOPWORDS)
    return ['#' + w for w, n in ranked.most_common(count) if n >= 2]


def _attribution_line(attributions):
    return ('Credits: ' + '; '.join(attributions)) if attributions else ''


def build_instagram_caption(body, hashtags, question, attributions):
    footer = '\n\n'.join(p for p in [_attribution_line(attributions), ' '.join(hashtags)] if p)
    head = '\n\n'.join(p for p in [body.strip(), (question or '').strip()] if p)
    room = INSTAGRAM_CAPTION_MAX - (len(footer) + 2 if footer else 0)
    if room < 1:
        raise MetadataError('Required attribution and hashtags alone exceed the 2200 character Instagram caption limit.')
    if len(head) > room:
        head = head[:room - 1].rstrip() + '…'
    return '\n\n'.join(p for p in [head, footer] if p)


def with_attribution(caption, attributions):
    """Append any required attribution that an edit removed; the caption stays within the platform limit."""
    missing = [a for a in attributions if a not in caption]
    if not missing:
        return caption
    line = _attribution_line(missing)
    if len(caption) + 2 + len(line) > INSTAGRAM_CAPTION_MAX:
        raise MetadataError('Required attribution does not fit in the 2200 character caption. Shorten the caption.')
    return caption.rstrip() + '\n\n' + line


# ---- validation --------------------------------------------------------------------------------------------------

def validate_instagram(meta):
    caption = str(meta.get('caption', '')).strip()
    if not caption:
        raise MetadataError('An Instagram caption is required.')
    if len(caption) > INSTAGRAM_CAPTION_MAX:
        raise MetadataError(f'Instagram captions are limited to {INSTAGRAM_CAPTION_MAX} characters (this one has {len(caption)}).')
    hashtags = normalize_hashtags(meta.get('hashtags', []), INSTAGRAM_MAX_HASHTAGS)
    if len(set(t.lower() for t in HASHTAG_RE.findall(caption)) | {t.lower() for t in hashtags}) > INSTAGRAM_MAX_HASHTAGS:
        raise MetadataError(f'Instagram allows at most {INSTAGRAM_MAX_HASHTAGS} hashtags per post.')
    question = str(meta.get('question', '') or '').strip()
    if len(question) > QUESTION_MAX:
        raise MetadataError(f'The engagement question is limited to {QUESTION_MAX} characters.')
    return {'caption': caption, 'hashtags': hashtags, 'question': question, 'share_to_feed': bool(meta.get('share_to_feed', True))}


def validate_youtube(meta):
    title = str(meta.get('title', '')).strip()
    if not title:
        raise MetadataError('A YouTube title is required.')
    if len(title) > YOUTUBE_TITLE_MAX:
        raise MetadataError(f'YouTube titles are limited to {YOUTUBE_TITLE_MAX} characters.')
    description = str(meta.get('description', ''))
    if len(description) > 5000:
        raise MetadataError('YouTube descriptions are limited to 5000 characters.')
    return {'title': title, 'description': description,
            'hashtags': normalize_hashtags(meta.get('hashtags', []), YOUTUBE_MAX_HASHTAGS),
            'privacy': meta.get('privacy', 'public'), 'altered_content': bool(meta.get('altered_content', False))}


def synthetic_narration(short):
    if short.kind != 'story' or not short.script_id or not short.script:
        return False
    asset = short.script.narration_asset
    return bool(asset and asset.origin == 'generated_tts')


# ---- builders ----------------------------------------------------------------------------------------------------

def build_metadata(short, platform, *, title='', description='', hashtags=None, scheduled=False, use_groq=False):
    """Return (metadata dict, source) where source is 'groq', 'stored' or 'extractive'. May call Groq when use_groq."""
    transcript = transcript_for(short)
    generated = None
    if use_groq and transcript:
        from ..services import groq
        if groq.enabled():
            generated = groq.generate_metadata(transcript)
    title = (generated or {}).get('title') or title or short.title or ''
    stored_body = clean_body((generated or {}).get('description') or description or short.description)
    tags = list((generated or {}).get('hashtags') or hashtags or short.hashtags or [])
    source = 'groq' if generated else 'stored'
    if not stored_body and transcript:
        stored_body, source = extractive_description(transcript), 'extractive'
    if not title and transcript:
        title, source = extractive_title(transcript), 'extractive'
    if not tags and transcript:
        tags = keyword_hashtags(transcript)
    attributions = attributions_for(short)
    if platform == 'youtube':
        tags = normalize_hashtags([t for t in tags if HASHTAG_RE.fullmatch('#' + str(t).lstrip('#'))], 200)[:YOUTUBE_MAX_HASHTAGS]
        body = (generated or {}).get('description') or description or short.description or stored_body
        missing = [a for a in attributions if a not in body]
        if missing:
            body = body.rstrip() + '\n\n' + _attribution_line(missing)
        meta = {'title': title.strip()[:YOUTUBE_TITLE_MAX], 'description': body[:5000], 'hashtags': tags,
                'privacy': 'private_until_publish_at' if scheduled else 'public',
                'altered_content': synthetic_narration(short)}
        return meta, source
    tags = normalize_hashtags([t for t in tags if HASHTAG_RE.fullmatch('#' + str(t).lstrip('#'))], 200)[:INSTAGRAM_MAX_HASHTAGS]
    question = ''
    meta = {'caption': build_instagram_caption(stored_body or title, tags, question, attributions), 'hashtags': tags,
            'question': question, 'share_to_feed': True}
    return meta, source
