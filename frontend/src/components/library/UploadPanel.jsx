import { useRef, useState } from 'react';
import { uploadAsset } from '../../api/assets';
import Button from '../ui/Button';
import Field from '../ui/Field';
import { LICENSE_OPTIONS, UPLOAD_KINDS, kindFromFile } from './licenses';
import { validateUpload } from './uploadRules';

const EMPTY = {
  kind: 'video',
  title: '',
  license_status: '',
  attribution_text: '',
  license_reference: '',
  rights_notes: '',
  tags: '',
};

/**
 * Upload form. The browser's fetch API exposes no byte-level progress, so the
 * panel shows a busy state until the server answers instead of a made-up percentage.
 */
export default function UploadPanel({ onUploaded }) {
  const fileInput = useRef(null);
  const [file, setFile] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [kindTouched, setKindTouched] = useState(false);
  const [errors, setErrors] = useState({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [serverError, setServerError] = useState('');

  const set = (key, value) => setForm(old => ({ ...old, [key]: value }));
  const selected = LICENSE_OPTIONS.find(option => option.value === form.license_status);

  function pickFile(event) {
    const next = event.target.files?.[0] || null;
    setFile(next);
    const guessed = kindFromFile(next);
    if (guessed && !kindTouched) set('kind', guessed);
  }

  async function submit(event) {
    event.preventDefault();
    setResult(null);
    setServerError('');
    const found = validateUpload(file, form);
    setErrors(found);
    if (Object.keys(found).length) return;

    const body = new FormData();
    body.append('file', file);
    Object.entries(form).forEach(([key, value]) => body.append(key, value));
    setBusy(true);
    try {
      const asset = await uploadAsset(body);
      setResult(asset);
      onUploaded?.(asset);
      if (!asset.deduplicated) {
        setForm(EMPTY);
        setFile(null);
        setKindTouched(false);
        if (fileInput.current) fileInput.current.value = '';
      }
    } catch (error) {
      setServerError(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="upload-panel" onSubmit={submit} noValidate aria-label="Upload an asset">
      <div className="form-grid">
        <Field label="File" error={errors.file}>
          <input ref={fileInput} type="file" accept="video/*,audio/*,image/*" onChange={pickFile} disabled={busy} />
        </Field>
        <Field label="Kind">
          <select
            value={form.kind}
            onChange={event => { setKindTouched(true); set('kind', event.target.value); }}
            disabled={busy}
          >
            {UPLOAD_KINDS.map(kind => <option key={kind.value} value={kind.value}>{kind.label}</option>)}
          </select>
        </Field>
        <Field label="Title">
          <input value={form.title} maxLength={300} onChange={event => set('title', event.target.value)} disabled={busy} />
        </Field>
        <Field label="Tags" hint="Comma separated, up to 30.">
          <input value={form.tags} onChange={event => set('tags', event.target.value)} disabled={busy} placeholder="boss-fight, intro" />
        </Field>
        <Field label="Licence status (required)" error={errors.license_status} hint={selected?.help} className="wide">
          <select
            value={form.license_status}
            required
            onChange={event => set('license_status', event.target.value)}
            disabled={busy}
          >
            <option value="">Choose a licence status…</option>
            {LICENSE_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
        </Field>
        {form.license_status === 'attribution_required' && (
          <Field label="Attribution text" error={errors.attribution_text} hint="Shown as a credit on posts that use this asset." className="wide">
            <input value={form.attribution_text} onChange={event => set('attribution_text', event.target.value)} disabled={busy} />
          </Field>
        )}
        <Field label="Licence reference" hint="Licence URL, receipt or permission email.">
          <input value={form.license_reference} onChange={event => set('license_reference', event.target.value)} disabled={busy} />
        </Field>
        <Field label="Notes">
          <input value={form.rights_notes} onChange={event => set('rights_notes', event.target.value)} disabled={busy} />
        </Field>
      </div>

      <div className="form-actions">
        <Button type="submit" variant="primary" busy={busy}>{busy ? 'Uploading…' : 'Upload to library'}</Button>
        <span role="status" aria-live="polite" className="form-status">
          {busy && 'Uploading and checking the file. Large files can take a while.'}
          {!busy && result?.deduplicated && `"${result.title}" is already in your library. Nothing new was added.`}
          {!busy && result && !result.deduplicated && `"${result.title}" was added to your library.`}
        </span>
      </div>
      {serverError && <p className="field-error" role="alert">{serverError}</p>}
    </form>
  );
}
