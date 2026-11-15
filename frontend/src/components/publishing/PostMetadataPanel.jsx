import { useEffect, useState } from 'react';
import { getPostMetadata, savePostMetadata } from '../../api/social';
import Button from '../ui/Button';
import Field from '../ui/Field';
import Spinner from '../ui/Spinner';
import { charCount, parseHashtags, validateMetadata } from './metadataRules';

function toDraft(platform, meta = {}) {
  const base = { hashtags: (meta.hashtags || []).join(' ') };
  if (platform === 'instagram') {
    return { ...base, caption: meta.caption || '', question: meta.question || '', share_to_feed: meta.share_to_feed !== false };
  }
  return { ...base, title: meta.title || '', description: meta.description || '', altered_content: !!meta.altered_content };
}

function Counter({ value, max }) {
  const count = charCount(value);
  return <small className={`counter ${count > max ? 'over' : ''}`} aria-live="polite">{count} / {max}</small>;
}

/**
 * Per-platform metadata for one post (GET/PUT /v1/social/posts/{id}/metadata).
 * Editable only while the post is a draft or failed; otherwise it is read-only.
 */
export default function PostMetadataPanel({ post, onSaved }) {
  const [loaded, setLoaded] = useState(null);
  const [draft, setDraft] = useState(null);
  const [state, setState] = useState('loading');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState('');
  const platform = post.platform;
  const editable = ['draft', 'failed'].includes(post.status);

  function apply(data) {
    setLoaded(data);
    setDraft(toDraft(platform, data.metadata));
  }

  useEffect(() => {
    let current = true;
    getPostMetadata(post.id)
      .then(data => {
        if (!current) return;
        apply(data);
        setState('ready');
      })
      .catch(e => {
        if (!current) return;
        setError(e.message);
        setState('error');
      });
    return () => { current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [post.id]);

  const limits = loaded?.limits;
  const errors = draft ? validateMetadata(platform, draft, limits) : {};
  const invalid = Object.keys(errors).length > 0;
  const set = (key, value) => { setDraft(old => ({ ...old, [key]: value })); setMessage(''); };

  async function regenerate() {
    setBusy('regenerate');
    setError('');
    setMessage('');
    try {
      const data = await getPostMetadata(post.id, { regenerate: true });
      apply(data);
      setMessage(`Regenerated from ${data.source}. Review it, then save to keep it.`);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  }

  async function save(event) {
    event.preventDefault();
    if (invalid) return;
    const hashtags = parseHashtags(draft.hashtags);
    const fields = platform === 'instagram'
      ? { caption: draft.caption, hashtags, question: draft.question, share_to_feed: draft.share_to_feed }
      : { title: draft.title, description: draft.description, hashtags, altered_content: draft.altered_content };
    setBusy('save');
    setError('');
    setMessage('');
    try {
      await savePostMetadata(post.id, fields);
      setMessage('Metadata saved.');
      onSaved?.();
      // Re-read so server-side changes (e.g. appended attribution) are shown.
      apply(await getPostMetadata(post.id));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy('');
    }
  }

  if (state === 'loading') return <p className="muted"><Spinner label="Loading metadata" /> Loading metadata…</p>;
  if (state === 'error') return <p className="field-error" role="alert">Could not load metadata: {error}</p>;

  const attribution = loaded.required_attribution || [];
  const max = { ...(platform === 'instagram' ? { caption: 2200, hashtags: 30, question: 200 } : { title: 100, hashtags: 5 }) };
  if (limits?.caption_max) max.caption = limits.caption_max;
  if (limits?.title_max) max.title = limits.title_max;
  if (limits?.hashtags_max) max.hashtags = limits.hashtags_max;

  return (
    <form className="metadata-panel" onSubmit={save} aria-label={`${platform === 'instagram' ? 'Instagram' : 'YouTube'} metadata`}>
      <p className="muted">
        Source: {loaded.source}.{' '}
        {!editable && 'This post can no longer be edited.'}
      </p>

      {platform === 'instagram' ? (
        <>
          <Field label="Caption" error={errors.caption}>
            <textarea rows={6} value={draft.caption} disabled={!editable} onChange={e => set('caption', e.target.value)} />
          </Field>
          <Counter value={draft.caption} max={max.caption} />
          <Field label="Engagement question (optional)" error={errors.question}>
            <input value={draft.question} disabled={!editable} onChange={e => set('question', e.target.value)} />
          </Field>
          <label className="check">
            <input type="checkbox" checked={draft.share_to_feed} disabled={!editable} onChange={e => set('share_to_feed', e.target.checked)} />
            <span>Also share to the profile feed</span>
          </label>
        </>
      ) : (
        <>
          <Field label="Title" error={errors.title}>
            <input value={draft.title} disabled={!editable} onChange={e => set('title', e.target.value)} />
          </Field>
          <Counter value={draft.title} max={max.title} />
          <Field label="Description" error={errors.description}>
            <textarea rows={5} value={draft.description} disabled={!editable} onChange={e => set('description', e.target.value)} />
          </Field>
          <label className="check">
            <input type="checkbox" checked={draft.altered_content} disabled={!editable} onChange={e => set('altered_content', e.target.checked)} />
            <span>Contains altered or synthetic content (for example generated narration)</span>
          </label>
        </>
      )}

      <Field label={`Hashtags (up to ${max.hashtags})`} error={errors.hashtags} hint="Separate with spaces or commas.">
        <input value={draft.hashtags} disabled={!editable} onChange={e => set('hashtags', e.target.value)} />
      </Field>

      {attribution.length > 0 && (
        <div className="attribution" aria-label="Required attribution">
          <strong>Required attribution</strong>
          <p className="muted">Added automatically because assets used in this video require credit. It cannot be removed.</p>
          <ul>{attribution.map(line => <li key={line}>{line}</li>)}</ul>
        </div>
      )}

      {editable && (
        <div className="form-actions">
          <Button type="submit" variant="primary" size="sm" disabled={invalid} busy={busy === 'save'}>Save metadata</Button>
          <Button size="sm" busy={busy === 'regenerate'} onClick={regenerate}>Regenerate</Button>
        </div>
      )}
      <p className="form-status" role="status" aria-live="polite">{message}</p>
      {error && <p className="field-error" role="alert">{error}</p>}
    </form>
  );
}
