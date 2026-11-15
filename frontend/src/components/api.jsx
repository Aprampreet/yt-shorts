const BASE = import.meta.env.VITE_API_BASE || '/api';
let csrf = '';

export async function initialize() {
  const response = await fetch(`${BASE}/v1/session`, { credentials: 'include' });
  if (!response.ok) throw new Error('Studio API is unavailable. Start the Django server.');
  csrf = (await response.json()).csrf_token;
}

export async function api(path, options = {}) {
  const response = await fetch(`${BASE}/v1${path}`, {
    credentials: 'include', ...options,
    headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }),
      'X-CSRFToken': csrf, ...options.headers },
  });
  let data;
  try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response.'); }
  if (!response.ok) {
    const error = new Error(data.message || (typeof data.detail === 'string' ? data.detail : 'Check the submitted fields and try again.'));
    // Extra context for callers that need it (for example the 409 `projects` list on asset deletion).
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

export function asset(path) {
  return BASE === '/api' ? path : `${BASE.replace(/\/api\/?$/, '')}${path}`;
}
