import { useEffect, useRef, useState } from 'react';
import { deleteAsset, updateAsset } from '../../api/assets';
import { asset as assetUrl } from '../api';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Field from '../ui/Field';
import LicenseBadge from './LicenseBadge';
import { LICENSE_OPTIONS, formatBytes, formatDuration, mediaClass } from './licenses';

function Player({ asset }) {
  const src = assetUrl(asset.file_url);
  const cls = mediaClass(asset.kind);
  if (cls === 'video') {
    return <video controls preload="metadata" src={src} poster={asset.thumbnail_url ? assetUrl(asset.thumbnail_url) : undefined} aria-label={`Preview ${asset.title}`} />;
  }
  if (cls === 'audio') return <audio controls preload="metadata" src={src} aria-label={`Preview ${asset.title}`} />;
  if (cls === 'image') return <img src={src} alt={asset.title || 'Asset preview'} />;
  return <a className="button secondary" href={src}>Download file</a>;
}

function toDraft(asset) {
  return {
    title: asset.title || '',
    tags: (asset.tags || []).join(', '),
    license_status: asset.license_status,
    attribution_text: asset.attribution_text || '',
    license_reference: asset.license_reference || '',
    rights_notes: asset.rights_notes || '',
    source_url: asset.source_url || '',
  };
}

/**
 * Preview drawer: player, metadata, usage, licence editing, pinning and
 * deletion. Deletion needs an explicit confirmation and lists the projects
 * when the backend refuses with 409 `asset_in_use`.
 */
export default function AssetDrawer({ asset, onClose, onChanged, onDeleted, onTagClick }) {
  const closeRef = useRef(null);
  const [draft, setDraft] = useState(() => toDraft(asset));
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [inUse, setInUse] = useState(null);

  useEffect(() => {
    const previous = document.activeElement;
    closeRef.current?.focus();
    const onKey = event => { if (event.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      if (previous instanceof HTMLElement) previous.focus();
    };
  }, [onClose]);

  const set = (key, value) => setDraft(old => ({ ...old, [key]: value }));
  const needsAttribution = draft.license_status === 'attribution_required' && !draft.attribution_text.trim();

  async function run(name, action, success) {
    setBusy(name);
    setError('');
    setMessage('');
    try {
      await action();
      if (success) setMessage(success);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  }

  function save(event) {
    event.preventDefault();
    if (needsAttribution) {
      setError('Attribution text is required for this licence status.');
      return;
    }
    const tags = draft.tags.split(',').map(tag => tag.trim()).filter(Boolean);
    return run('save', async () => {
      const updated = await updateAsset(asset.id, { ...draft, tags });
      onChanged({ ...updated, usage: updated.usage || asset.usage });
      setDraft(toDraft(updated));
    }, 'Changes saved.');
  }

  const togglePin = () => run('pin', async () => {
    const updated = await updateAsset(asset.id, { pinned: !asset.pinned });
    onChanged(updated);
  }, asset.pinned ? 'Unpinned.' : 'Pinned. Pinned assets stay in your library.');

  async function confirmDelete() {
    setBusy('delete');
    setError('');
    setInUse(null);
    try {
      await deleteAsset(asset.id);
      onDeleted(asset);
    } catch (e) {
      if (e.status === 409 && e.data?.error === 'asset_in_use') {
        setInUse({ message: e.message, projects: e.data.projects || [] });
      } else {
        setError(e.message);
      }
      setConfirming(false);
    } finally {
      setBusy('');
    }
  }

  const usage = asset.usage || {};

  return (
    <div className="drawer-backdrop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label={`Asset: ${asset.title || 'Untitled'}`}>
        <header className="drawer-head">
          <div>
            <span className="eyebrow">{asset.kind.toUpperCase()}</span>
            <h2>{asset.title || 'Untitled'}</h2>
          </div>
          <button ref={closeRef} type="button" className="icon-button" onClick={onClose} aria-label="Close preview">×</button>
        </header>

        <div className={`drawer-preview ${mediaClass(asset.kind)}`}>
          <Player asset={asset} />
        </div>

        <div className="drawer-body">
          <div className="badge-row">
            <LicenseBadge asset={asset} />
            {asset.pinned && <Badge tone="accent">Pinned</Badge>}
          </div>

          <dl className="meta-list">
            <div><dt>Size</dt><dd>{formatBytes(asset.size_bytes)}</dd></div>
            {asset.duration != null && <div><dt>Duration</dt><dd>{formatDuration(asset.duration)}</dd></div>}
            {asset.width && <div><dt>Dimensions</dt><dd>{asset.width} × {asset.height}</dd></div>}
            <div><dt>Origin</dt><dd>{asset.origin.replace(/_/g, ' ')}</dd></div>
            <div><dt>Added</dt><dd>{new Date(asset.created_at).toLocaleDateString()}</dd></div>
            <div>
              <dt>Usage</dt>
              <dd>{usage.projects || 0} projects · {usage.scenes || 0} scenes · {usage.narrations || 0} narrations</dd>
            </div>
          </dl>

          {asset.tags?.length > 0 && (
            <div className="tag-row" aria-label="Tags">
              {asset.tags.map(tag => (
                <button key={tag} type="button" className="tag" onClick={() => onTagClick(tag)}>#{tag}</button>
              ))}
            </div>
          )}

          <form className="drawer-form" onSubmit={save}>
            <h3>Edit details</h3>
            <Field label="Title"><input value={draft.title} maxLength={300} onChange={e => set('title', e.target.value)} /></Field>
            <Field label="Tags" hint="Comma separated.">
              <input value={draft.tags} onChange={e => set('tags', e.target.value)} />
            </Field>
            <Field label="Licence status" hint={LICENSE_OPTIONS.find(o => o.value === draft.license_status)?.help}>
              <select value={draft.license_status} onChange={e => set('license_status', e.target.value)}>
                {LICENSE_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
            </Field>
            <Field
              label={draft.license_status === 'attribution_required' ? 'Attribution text (required)' : 'Attribution text'}
              error={needsAttribution ? 'Required for this licence status.' : ''}
            >
              <input value={draft.attribution_text} onChange={e => set('attribution_text', e.target.value)} />
            </Field>
            <Field label="Licence reference">
              <input value={draft.license_reference} onChange={e => set('license_reference', e.target.value)} />
            </Field>
            <Field label="Source URL"><input type="url" value={draft.source_url} onChange={e => set('source_url', e.target.value)} /></Field>
            <Field label="Notes"><textarea rows={3} value={draft.rights_notes} onChange={e => set('rights_notes', e.target.value)} /></Field>
            <div className="form-actions">
              <Button type="submit" variant="primary" size="sm" busy={busy === 'save'}>Save changes</Button>
              <Button size="sm" onClick={togglePin} busy={busy === 'pin'}>{asset.pinned ? 'Unpin' : 'Pin'}</Button>
            </div>
          </form>

          <p className="form-status" role="status" aria-live="polite">{message}</p>
          {error && <p className="field-error" role="alert">{error}</p>}

          {inUse && (
            <div className="in-use" role="alert">
              <strong>This asset is in use and cannot be deleted.</strong>
              <p>{inUse.message}</p>
              {inUse.projects.length > 0 && (
                <ul>
                  {inUse.projects.map(project => <li key={project.id}>{project.title || project.id}</li>)}
                </ul>
              )}
            </div>
          )}

          <div className="danger-zone">
            {confirming ? (
              <div role="alertdialog" aria-label="Confirm deletion">
                <p>Remove &ldquo;{asset.title || 'Untitled'}&rdquo; from your library? It will no longer appear in lists or pickers.</p>
                <div className="form-actions">
                  <Button variant="danger" size="sm" busy={busy === 'delete'} onClick={confirmDelete}>Confirm delete</Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Keep asset</Button>
                </div>
              </div>
            ) : (
              <Button variant="danger" size="sm" onClick={() => { setInUse(null); setConfirming(true); }}>Delete asset</Button>
            )}
          </div>
        </div>
      </aside>
    </div>
  );
}
