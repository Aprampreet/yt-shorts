import { useEffect } from 'react';
import { api } from '../components/api';

/** Job statuses after which the worker will not change the job again. */
export const TERMINAL_STATUSES = ['failed', 'cancelled', 'completed', 'needs_review'];

export const isTerminal = status => TERMINAL_STATUSES.includes(status);

/**
 * Polls the active job until it reaches a terminal status.
 *
 * Timing is unchanged from the original studio: first poll after 1 s, then
 * every 2 s. Progress comes only from the server; nothing is simulated.
 *
 * Callbacks should be stable (state setters or useCallback), otherwise the
 * polling loop restarts on every render.
 *
 * @param {string|undefined} jobId
 * @param {string|undefined} status
 * @param {{ onUpdate: (job: object) => void, onTerminal: () => Promise<void>, onError: (message: string) => void }} handlers
 */
export default function useJobPolling(jobId, status, { onUpdate, onTerminal, onError }) {
  useEffect(() => {
    if (!jobId || isTerminal(status)) return;
    let stopped = false;
    let timer;
    async function poll() {
      try {
        const next = await api(`/jobs/${jobId}`);
        if (!stopped) {
          onUpdate(next);
          if (isTerminal(next.status)) {
            await onTerminal();
            return;
          }
        }
      } catch (e) {
        if (!stopped) onError(e.message);
      }
      if (!stopped) timer = setTimeout(poll, 2000);
    }
    timer = setTimeout(poll, 1000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [jobId, status, onUpdate, onTerminal, onError]);
}
