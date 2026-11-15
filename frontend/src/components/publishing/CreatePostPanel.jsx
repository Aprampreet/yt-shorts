import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import { approvePost, createPost, getAccounts } from '../../api/social';
import Button from '../ui/Button';
import Field from '../ui/Field';
import Spinner from '../ui/Spinner';
import { finishedVideos } from './finishedVideos';
import { isFarEnough, toOffsetISO } from './datetime';
import PostStatusBadge from './PostStatusBadge';
import RightsReview from './RightsReview';

function browserTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/**
 * Create a post from a finished video: pick the video, platform, connected
 * account and optional time (POST /v1/social/posts), then approve it.
 * The result is shown with the status the API returns.
 */
export default function CreatePostPanel({ platforms, onChanged }) {
  const [videos, setVideos] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [state, setState] = useState('loading');
  const [loadError, setLoadError] = useState('');
  const [shortId, setShortId] = useState('');
  const [platform, setPlatform] = useState('youtube');
  const [accountId, setAccountId] = useState('');
  const [when, setWhen] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [fieldError, setFieldError] = useState('');
  const [busy, setBusy] = useState('');
  const [created, setCreated] = useState(null);
  const [actionError, setActionError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let current = true;
    setState('loading');
    Promise.all([api('/jobs'), getAccounts()])
      .then(([jobs, accts]) => {
        if (!current) return;
        setVideos(finishedVideos(jobs));
        setAccounts(Array.isArray(accts) ? accts : []);
        setState('ready');
      })
      .catch(e => {
        if (!current) return;
        setLoadError(e.message);
        setState('error');
      });
    return () => { current = false; };
  }, [attempt]);

  const chosen = useMemo(() => videos.find(item => item.short.id === shortId)?.short, [videos, shortId]);
  const platformAccounts = accounts.filter(account => account.platform === platform);
  const instagram = platforms?.instagram;
  const instagramBlocked = !!instagram && instagram.configured === false;

  function pickVideo(id) {
    setShortId(id);
    const short = videos.find(item => item.short.id === id)?.short;
    setTitle(short?.title || '');
    setDescription(short?.description || '');
  }

  async function submit(event) {
    event.preventDefault();
    setFieldError('');
    setActionError('');
    if (!chosen) return setFieldError('Choose a finished video.');
    if (!accountId) return setFieldError(`Choose a connected ${platform === 'youtube' ? 'YouTube' : 'Instagram'} account.`);
    if (!title.trim()) return setFieldError('A title is required.');
    if (when && !isFarEnough(when)) return setFieldError('Pick a time at least ten minutes from now.');

    const body = {
      short_id: chosen.id,
      platform,
      account_id: accountId,
      title: title.trim(),
      description,
      hashtags: (chosen.hashtags || []).slice(0, platform === 'youtube' ? 5 : 30),
      timezone: browserTimeZone(),
    };
    if (when) body.scheduled_at = toOffsetISO(when);

    setBusy('create');
    try {
      setCreated(await createPost(body));
      onChanged?.();
    } catch (e) {
      setActionError(e.message);
    } finally {
      setBusy('');
    }
  }

  async function approve() {
    setBusy('approve');
    setActionError('');
    try {
      setCreated(await approvePost(created.id));
      onChanged?.();
    } catch (e) {
      setActionError(e.message);
    } finally {
      setBusy('');
    }
  }

  return (
    <section className="panel" aria-labelledby="create-post-title" aria-busy={state === 'loading'}>
      <h2 id="create-post-title" className="panel-title">Create a post from a finished video</h2>

      {state === 'loading' && <p className="muted"><Spinner label="Loading videos" /> Loading finished videos…</p>}
      {state === 'error' && (
        <div className="error-banner" role="alert">
          Could not load videos: {loadError}
          <Button size="sm" variant="ghost" onClick={() => setAttempt(n => n + 1)}>Retry</Button>
        </div>
      )}
      {state === 'ready' && videos.length === 0 && (
        <p className="muted">No finished videos yet. Generate Shorts first, then come back to publish them.</p>
      )}

      {state === 'ready' && videos.length > 0 && (
        <form className="form-grid" onSubmit={submit} noValidate>
          <Field label="Finished video" className="wide">
            <select value={shortId} onChange={e => pickVideo(e.target.value)}>
              <option value="">Choose a video…</option>
              {videos.map(({ short, jobTitle }) => (
                <option key={short.id} value={short.id}>
                  {jobTitle} · Short {short.short_number} · {short.title} ({Math.round(short.duration)}s)
                </option>
              ))}
            </select>
          </Field>
          <Field label="Platform" hint={platform === 'instagram' && instagram?.reason ? instagram.reason : undefined}>
            <select value={platform} onChange={e => { setPlatform(e.target.value); setAccountId(''); }}>
              <option value="youtube">YouTube</option>
              <option value="instagram" disabled={instagramBlocked}>
                Instagram{instagramBlocked ? ' (not configured)' : ''}
              </option>
            </select>
          </Field>
          <Field
            label="Connected account"
            hint={platformAccounts.length ? undefined : 'No connected account for this platform yet.'}
          >
            <select value={accountId} onChange={e => setAccountId(e.target.value)}>
              <option value="">Choose an account…</option>
              {platformAccounts.map(account => <option key={account.id} value={account.id}>{account.account_name}</option>)}
            </select>
          </Field>
          <Field label="Title" className="wide">
            <input value={title} onChange={e => setTitle(e.target.value)} />
          </Field>
          <Field label="Description" className="wide">
            <textarea rows={3} value={description} onChange={e => setDescription(e.target.value)} />
          </Field>
          <Field label="Publish at (optional)" hint={`Your time zone: ${browserTimeZone()}. Leave empty to publish right after approval. Must be at least 10 minutes ahead.`}>
            <input type="datetime-local" value={when} onChange={e => setWhen(e.target.value)} />
          </Field>
          <div className="form-actions wide">
            <Button type="submit" variant="primary" busy={busy === 'create'}>Create draft</Button>
          </div>
          {fieldError && <p className="field-error wide" role="alert">{fieldError}</p>}
        </form>
      )}

      {actionError && <p className="field-error" role="alert">{actionError}</p>}

      {created && (
        <div className="created-post" role="status" aria-live="polite">
          <p>
            <strong>{created.title}</strong> · {created.platform} · <PostStatusBadge status={created.status} />
          </p>
          {created.status === 'draft' && (
            <>
              <p className="muted">
                Review the metadata in the queue below. Approving needs a documented rights review.
              </p>
              {chosen && <RightsReview shortId={chosen.id} initialStatus={chosen.rights_review_status} />}
              <Button variant="primary" size="sm" busy={busy === 'approve'} onClick={approve}>Approve for publishing</Button>
            </>
          )}
          {created.status === 'queued' && <p className="muted">Approved and queued. It is not published until the platform confirms.</p>}
          {created.status === 'scheduled' && <p className="muted">Approved and scheduled. It is not published until its time arrives.</p>}
        </div>
      )}
    </section>
  );
}
