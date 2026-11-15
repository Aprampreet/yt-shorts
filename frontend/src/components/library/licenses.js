/** The six licence states the backend accepts, with plain-language guidance. */
export const LICENSE_OPTIONS = [
  { value: 'owned', label: 'Owned', help: 'You created it or hold all rights. No credit is required.' },
  {
    value: 'commercial_use_permitted',
    label: 'Commercial use permitted',
    help: 'The licence or the rights holder allows commercial use without credit.',
  },
  {
    value: 'attribution_required',
    label: 'Attribution required',
    help: 'You may use it if you credit the creator. Credit text is required and is added to posts that use it.',
  },
  {
    value: 'permission_pending',
    label: 'Permission pending',
    help: 'You have asked for permission but not received it. Publishing is blocked until this changes.',
  },
  { value: 'unknown', label: 'Unknown', help: 'You are not sure where it came from. Publishing is blocked until this is resolved.' },
  { value: 'restricted', label: 'Restricted', help: 'Use is not allowed. Keep it only for reference; it cannot be published.' },
];

export const UPLOAD_KINDS = [
  { value: 'video', label: 'Video' },
  { value: 'image', label: 'Image' },
  { value: 'graphic', label: 'Graphic' },
  { value: 'audio', label: 'Audio' },
  { value: 'music', label: 'Music' },
  { value: 'sfx', label: 'Sound effect' },
  { value: 'narration', label: 'Narration' },
];

export const FILTER_KINDS = [...UPLOAD_KINDS, { value: 'subtitle', label: 'Subtitle' }];

const AUDIO = new Set(['audio', 'music', 'sfx', 'narration']);
const IMAGE = new Set(['image', 'graphic']);

/** 'video' | 'audio' | 'image' | 'other' — decides which player to render. */
export function mediaClass(kind) {
  if (kind === 'video') return 'video';
  if (AUDIO.has(kind)) return 'audio';
  if (IMAGE.has(kind)) return 'image';
  return 'other';
}

export function kindFromFile(file) {
  const type = file?.type || '';
  if (type.startsWith('video/')) return 'video';
  if (type.startsWith('audio/')) return 'audio';
  if (type.startsWith('image/')) return 'image';
  return '';
}

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return '';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return '';
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}
