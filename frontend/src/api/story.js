import { api } from '../components/api';

const json = (method, body) => ({ method, body: JSON.stringify(body ?? {}) });

/** GET /v1/story/projects → { projects: [...] } */
export function listProjects() {
  return api('/story/projects');
}

/** POST /v1/story/projects → 201 project detail. */
export function createProject(body) {
  return api('/story/projects', json('POST', body));
}

export function getProject(id) {
  return api(`/story/projects/${id}`);
}

/** PUT /v1/story/projects/{id}: title, music_asset_id ('' clears), theme, caption_preset, video_count. */
export function updateProject(id, patch) {
  return api(`/story/projects/${id}`, json('PUT', patch));
}

/**
 * DELETE /v1/story/projects/{id} answers 204 with no body, and the shared `api()` helper
 * cannot parse an empty body. A parse failure carries no `status`, so it is treated as
 * success here; the caller reloads the list to confirm.
 */
export async function deleteProject(id) {
  try {
    await api(`/story/projects/${id}`, { method: 'DELETE' });
  } catch (error) {
    if (error.status) throw error;
  }
}

export const analyzeProject = id => api(`/story/projects/${id}/analyze`, json('POST'));

export function attachEvidence(projectId, claimId, body) {
  return api(`/story/projects/${projectId}/claims/${claimId}`, json('PUT', body));
}

export function removeEvidence(projectId, claimId) {
  return api(`/story/projects/${projectId}/claims/${claimId}/evidence`, { method: 'DELETE' });
}

export const generateConcepts = id => api(`/story/projects/${id}/concepts/generate`, json('POST'));

export function selectConcepts(id, conceptIds) {
  return api(`/story/projects/${id}/concepts/select`, json('POST', { concept_ids: conceptIds }));
}

export function generateScripts(id, force = false) {
  return api(`/story/projects/${id}/scripts/generate`, json('POST', { force }));
}

/** POST scripts/manual with `{ title, language, sections }` or `{ title, language, full_text }`. */
export function createManualScript(id, body) {
  return api(`/story/projects/${id}/scripts/manual`, json('POST', body));
}

export function editScript(scriptId, body) {
  return api(`/story/scripts/${scriptId}`, json('PUT', body));
}

export const regenerateScript = scriptId => api(`/story/scripts/${scriptId}/regenerate`, json('POST'));

export function approveScript(scriptId, overrideQuality = false) {
  const query = overrideQuality ? '?override_quality=true' : '';
  return api(`/story/scripts/${scriptId}/approve${query}`, json('POST'));
}

export function narrateScript(scriptId, { provider, voice_id, speed }) {
  return api(`/story/scripts/${scriptId}/narrate`, json('POST', { provider, voice_id, speed }));
}

export function attachNarration(scriptId, assetId) {
  return api(`/story/scripts/${scriptId}/narration`, json('POST', { asset_id: assetId }));
}

export function buildStoryboard(scriptId, { asset_ids = [], use_source_clips = false } = {}) {
  return api(`/story/scripts/${scriptId}/storyboard`, json('POST', { asset_ids, use_source_clips }));
}

export const listScenes = scriptId => api(`/story/scripts/${scriptId}/scenes`);

export function editScene(sceneId, changes) {
  return api(`/story/scenes/${sceneId}`, json('PUT', changes));
}

export function renderProject(id, scriptIds) {
  return api(`/story/projects/${id}/render`, json('POST', { script_ids: scriptIds }));
}

export const getJob = id => api(`/jobs/${id}`);

export const cancelJob = id => api(`/jobs/${id}/cancel`, json('POST'));
