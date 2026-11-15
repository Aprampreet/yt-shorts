import { api } from '../components/api';

/** GET /v1/capabilities (no session needed). */
export function getCapabilities() {
  return api('/capabilities');
}

/** GET /v1/providers */
export function getProviders() {
  return api('/providers');
}
