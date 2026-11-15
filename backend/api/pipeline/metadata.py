"""Grounded title/description/hashtags: Groq -> editorial analysis -> extractive fallback."""
from ..services.groq import finalize_description, generate_metadata


def clip_metadata(text, editorial, source_title, number):
    """Return (title, description, hashtags, groq_metadata) for one rendered clip."""
    title = editorial['title_candidates'][0][:500] if editorial else (text[:85] or f'{source_title[:75]} · Highlight {number}')
    groq_metadata = generate_metadata(text)
    if groq_metadata:
        title = groq_metadata['title']
    hashtags = groq_metadata['hashtags'] if groq_metadata else editorial['hashtags'] if editorial else ['#Shorts']
    base_description = groq_metadata['description'] if groq_metadata else editorial['description'] if editorial else (
        'A standout moment from this Short: ' + (text[:220].rstrip() or 'watch the moment unfold.'))
    description = finalize_description(base_description, hashtags)
    return title, description, hashtags, groq_metadata
