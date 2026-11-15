import { useEffect, useState } from 'react';
import { getCapabilities } from '../../api/providers';
import Badge from '../ui/Badge';

/**
 * One-line strip from GET /v1/capabilities explaining whether captions can be
 * transcribed and whether hosted AI is on. Renders nothing until it has an
 * answer; a failed request is reported rather than guessed.
 */
export default function ProviderStatus() {
  const [caps, setCaps] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let current = true;
    getCapabilities()
      .then(data => { if (current) setCaps(data); })
      .catch(e => { if (current) setError(e.message); });
    return () => { current = false; };
  }, []);

  if (error) {
    return <p className="provider-status muted" role="status">Capability status unavailable: {error}</p>;
  }
  if (!caps || typeof caps.asr !== 'boolean') return null;

  return (
    <div className="provider-status" role="status" aria-label="Provider status">
      <span>
        <Badge tone={caps.asr ? 'success' : 'warn'}>Transcription {caps.asr ? 'available' : 'unavailable'}</Badge>
      </span>
      <span>
        <Badge tone={caps.hosted_inference_enabled ? 'accent' : 'neutral'}>
          Hosted AI {caps.hosted_inference_enabled ? 'on' : 'off'}
        </Badge>
      </span>
      {caps.reason && <span className="muted">{caps.reason}</span>}
    </div>
  );
}
