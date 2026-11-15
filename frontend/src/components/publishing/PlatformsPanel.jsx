import { useEffect, useState } from 'react';
import { connectInstagram, getPlatforms } from '../../api/social';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Spinner from '../ui/Spinner';

const PLATFORMS = [
  { key: 'youtube', label: 'YouTube' },
  { key: 'instagram', label: 'Instagram' },
];

function Flag({ label, on }) {
  return <Badge tone={on ? 'success' : 'neutral'}>{label}: {on ? 'yes' : 'no'}</Badge>;
}

/**
 * What can be published where, straight from GET /v1/social/platforms, with
 * the backend's own `reason` whenever something is unavailable.
 * YouTube is connected from the accounts panel below; Instagram has its own button here.
 */
export default function PlatformsPanel({ onLoaded }) {
  const [platforms, setPlatforms] = useState(null);
  const [state, setState] = useState('loading');
  const [error, setError] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    setState('loading');
    getPlatforms()
      .then(data => {
        if (!current) return;
        setPlatforms(data);
        onLoaded?.(data);
        setState('ready');
      })
      .catch(e => {
        if (!current) return;
        setError(e.message);
        setState('error');
      });
    return () => { current = false; };
  }, [attempt, onLoaded]);

  async function connect() {
    setConnecting(true);
    setError('');
    try {
      window.location.href = await connectInstagram();
    } catch (e) {
      setError(e.message);
      setConnecting(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="platforms-title" aria-busy={state === 'loading'}>
      <h2 id="platforms-title" className="panel-title">Platforms</h2>
      {state === 'loading' && <p className="muted"><Spinner label="Loading platform status" /> Checking platforms…</p>}
      {state === 'error' && (
        <div className="error-banner" role="alert">
          Could not load platform status: {error}
          <Button size="sm" variant="ghost" onClick={() => setAttempt(n => n + 1)}>Retry</Button>
        </div>
      )}
      {state === 'ready' && (
        <ul className="platform-list">
          {PLATFORMS.map(({ key, label }) => {
            const info = platforms?.[key];
            return (
              <li key={key} className="platform-row">
                <div className="platform-head">
                  <strong>{label}</strong>
                  {info ? (
                    <span className="badge-row">
                      <Flag label="Configured" on={info.configured} />
                      <Flag label="Connected" on={info.connected} />
                      <Flag label="Can publish" on={info.can_publish} />
                    </span>
                  ) : (
                    <Badge>Status not reported</Badge>
                  )}
                </div>
                {info?.reason && <p className="platform-reason">{info.reason}</p>}
                {key === 'instagram' && info?.configured && !info.connected && (
                  <Button size="sm" variant="primary" busy={connecting} onClick={connect}>Connect Instagram</Button>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {state === 'ready' && error && <p className="field-error" role="alert">{error}</p>}
    </section>
  );
}
