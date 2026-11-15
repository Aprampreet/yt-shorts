import { useEffect, useState } from 'react';
import { documentRightsReview, getShort } from '../../api/social';
import Badge from '../ui/Badge';
import Button from '../ui/Button';

/**
 * Documents the final rights review for a video. The API wants two separate
 * confirmations (source permission and music rights) and approval of any post
 * is blocked until both are on file. This records the user's own declaration;
 * it does not verify anything.
 */
export default function RightsReview({ shortId, initialStatus, onDocumented }) {
  const [status, setStatus] = useState(initialStatus);
  const [permission, setPermission] = useState(false);
  const [music, setMusic] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (initialStatus !== undefined) return undefined;
    let current = true;
    getShort(shortId)
      .then(short => { if (current) setStatus(short?.rights_review_status || ''); })
      .catch(() => { if (current) setStatus(''); });
    return () => { current = false; };
  }, [shortId, initialStatus]);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const short = await documentRightsReview(shortId, { permission, music });
      setStatus(short?.rights_review_status || 'rights_documented');
      onDocumented?.(short);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (status === 'rights_documented') {
    return <Badge tone="success">Rights review documented</Badge>;
  }

  return (
    <form className="rights-review" onSubmit={submit} aria-label="Rights review">
      <h4>Rights review</h4>
      <p className="muted">Watch the final video, then confirm both points. Approval stays blocked until you do.</p>
      <label className="check">
        <input type="checkbox" checked={permission} onChange={e => setPermission(e.target.checked)} />
        <span>I have permission to use the source footage and any third-party content in this video.</span>
      </label>
      <label className="check">
        <input type="checkbox" checked={music} onChange={e => setMusic(e.target.checked)} />
        <span>I have the rights to all music and sound in this video.</span>
      </label>
      <Button type="submit" size="sm" disabled={!permission || !music} busy={busy}>Document rights review</Button>
      {error && <p className="field-error" role="alert">{error}</p>}
    </form>
  );
}
