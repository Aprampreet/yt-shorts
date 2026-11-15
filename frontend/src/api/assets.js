import { api } from '../components/api';
import { queryString } from './query';

/** GET /v1/assets → { total, limit, offset, items } */
export function listAssets({ q, kind, license_status, tag, limit, offset } = {}) {
  return api(`/assets${queryString({ q, kind, license_status, tag, limit, offset })}`);
}

/** POST /v1/assets (multipart). 201 for a new asset, 200 with `deduplicated: true` for an existing one. */
export function uploadAsset(formData) {
  return api('/assets', { method: 'POST', body: formData });
}

export function updateAsset(id, patch) {
  return api(`/assets/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });
}

/** DELETE /v1/assets/{id}. Rejects with `error.status === 409` and `error.data.projects` when in use. */
export function deleteAsset(id) {
  return api(`/assets/${id}`, { method: 'DELETE' });
}
