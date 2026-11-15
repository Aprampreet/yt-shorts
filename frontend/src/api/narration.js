import { api } from '../components/api';
import { queryString } from './query';

/** GET /v1/narration/voices → { providers: [...], manual_upload: true } */
export function listVoices(language) {
  return api(`/narration/voices${queryString({ language })}`);
}

/** POST /v1/narration/preview → asset (play `file_url`). Rate limits answer 429 with `retry_after_seconds`. */
export function previewVoice({ language, voice_id, speed, provider }) {
  return api('/narration/preview', { method: 'POST', body: JSON.stringify({ language, voice_id, speed, provider }) });
}

/** POST /v1/narration/upload (multipart: file, i_recorded_this, title). */
export function uploadNarration(formData) {
  return api('/narration/upload', { method: 'POST', body: formData });
}
