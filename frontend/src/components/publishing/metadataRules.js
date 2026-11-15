const HASHTAG = /^#[\p{L}\p{M}\p{N}_]{1,60}$/u;

/** "#a, b  #c" → ['#a', '#b', '#c'] (deduplicated, case-insensitive). */
export function parseHashtags(text) {
  const out = [];
  (text || '').split(/[\s,]+/).forEach(raw => {
    if (!raw) return;
    const tag = `#${raw.replace(/^#+/, '')}`;
    if (!out.some(existing => existing.toLowerCase() === tag.toLowerCase())) out.push(tag);
  });
  return out;
}

/** Character count the way the platforms count it (code points, not UTF-16 units). */
export const charCount = text => [...(text || '')].length;

const DEFAULT_LIMITS = {
  youtube: { title_max: 100, hashtags_max: 5, description_max: 5000 },
  instagram: { caption_max: 2200, hashtags_max: 30, question_max: 200 },
};

/** Returns a map of field → message. Mirrors the server rules so mistakes show before saving. */
export function validateMetadata(platform, draft, limits = {}) {
  const max = { ...DEFAULT_LIMITS[platform], ...limits };
  const errors = {};
  const tags = parseHashtags(draft.hashtags);
  const badTag = tags.find(tag => !HASHTAG.test(tag));
  if (badTag) errors.hashtags = `"${badTag}" is not a valid hashtag (letters, digits and underscores only).`;
  else if (tags.length > max.hashtags_max) errors.hashtags = `At most ${max.hashtags_max} hashtags are allowed.`;

  if (platform === 'instagram') {
    const length = charCount(draft.caption);
    if (!draft.caption.trim()) errors.caption = 'A caption is required.';
    else if (length > max.caption_max) errors.caption = `Captions are limited to ${max.caption_max} characters (this one has ${length}).`;
    if (charCount(draft.question) > max.question_max) errors.question = `The question is limited to ${max.question_max} characters.`;
  } else {
    const length = charCount(draft.title);
    if (!draft.title.trim()) errors.title = 'A title is required.';
    else if (length > max.title_max) errors.title = `Titles are limited to ${max.title_max} characters (this one has ${length}).`;
    if (charCount(draft.description) > max.description_max) errors.description = `Descriptions are limited to ${max.description_max} characters.`;
  }
  return errors;
}
