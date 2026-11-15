import { useEffect, useState } from 'react';
import { listAssets } from '../../api/assets';
import LicenseBadge from '../library/LicenseBadge';

/**
 * Picks the second video for split-screen from the user's library. A second
 * video does not clear rights for the first, so the warning is always shown.
 */
export default function SecondaryAssetPicker({ value, onChange, disabled }) {
  const [assets, setAssets] = useState([]);
  const [state, setState] = useState('loading');
  const [error, setError] = useState('');

  useEffect(() => {
    let current = true;
    listAssets({ kind: 'video', limit: 100 })
      .then(data => {
        if (!current) return;
        setAssets(Array.isArray(data?.items) ? data.items : []);
        setState('ready');
      })
      .catch(e => {
        if (!current) return;
        setError(e.message);
        setState('error');
      });
    return () => { current = false; };
  }, []);

  const selected = assets.find(item => item.id === value);

  return (
    <div className="wide secondary-asset">
      <label htmlFor="secondary-asset">Second video (library)</label>
      <select
        id="secondary-asset"
        value={value || ''}
        disabled={disabled || state !== 'ready'}
        onChange={event => onChange(event.target.value || null)}
        aria-describedby="secondary-asset-note"
      >
        <option value="">
          {state === 'loading' ? 'Loading library…' : 'No second video'}
        </option>
        {assets.map(item => <option key={item.id} value={item.id}>{item.title || 'Untitled'}</option>)}
      </select>
      {state === 'error' && <small className="field-error" role="alert">Could not load your library: {error}</small>}
      {state === 'ready' && assets.length === 0 && (
        <small className="field-hint">No video assets in your library yet. Add one on the Media Library page.</small>
      )}
      {selected && <LicenseBadge asset={selected} />}
      <small id="secondary-asset-note" className="field-hint">
        A second video does not clear rights for your first video. You need permission for both.
      </small>
    </div>
  );
}
