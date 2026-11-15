import { api } from '../components/api';

export function getPlatforms() {
  return api('/social/platforms');
}

export function getAccounts() {
  return api('/social/accounts');
}

export async function connectInstagram() {
  const result = await api('/social/instagram/connect');
  return result.authorization_url;
}

export function getPostMetadata(postId, { regenerate = false } = {}) {
  return api(`/social/posts/${postId}/metadata${regenerate ? '?regenerate=true' : ''}`);
}

export function savePostMetadata(postId, fields) {
  return api(`/social/posts/${postId}/metadata`, { method: 'PUT', body: JSON.stringify(fields) });
}

export function createPost(data) {
  return api('/social/posts', { method: 'POST', body: JSON.stringify(data) });
}

export function approvePost(postId) {
  return api(`/social/posts/${postId}/approve`, { method: 'POST' });
}

export function getShort(shortId) {
  return api(`/shorts/${shortId}`);
}

/** The backend reads both confirmations from the query string, not the body. */
export function documentRightsReview(shortId, { permission, music }) {
  const query = `permission_verified=${permission ? 'true' : 'false'}&music_verified=${music ? 'true' : 'false'}`;
  return api(`/shorts/${shortId}/rights-review?${query}`, { method: 'POST' });
}
