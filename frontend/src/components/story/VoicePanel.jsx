import { useEffect, useMemo, useState } from 'react';
import { asset } from '../api';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Field from '../ui/Field';
import LicenseBadge from '../library/LicenseBadge';
import ErrorNote from './ErrorNote';
import useAction from './useAction';
import { BILLING_LABELS } from './constants';
import { attachNarration, narrateScript } from '../../api/story';
import { listVoices, previewVoice, uploadNarration } from '../../api/narration';

const commercialText = value => (value === true ? 'Commercial use allowed' : value === false ? 'Commercial use not allowed' : 'Commercial use: unknown');

function ProviderCard({ provider }) {
  return (
    <li className="provider-card">
      <div className="badge-row">
        <strong>{provider.label}</strong>
        <Badge tone={provider.available ? 'success' : 'neutral'}>{provider.available ? 'Available' : 'Unavailable'}</Badge>
        <Badge>{BILLING_LABELS[provider.billing] || provider.billing}</Badge>
        <Badge tone={provider.commercial_use === false ? 'warn' : 'neutral'}>{commercialText(provider.commercial_use)}</Badge>
      </div>
      <p className="muted">Languages: {(provider.languages || []).join(', ')}</p>
      {!provider.available && provider.reason && <p className="platform-reason">{provider.reason}</p>}
      {provider.notes && <p className="muted">{provider.notes}</p>}
    </li>
  );
}

function CurrentNarration({ narration }) {
  if (!narration) return null;
  return (
    <div className="story-callout" aria-label="Current narration">
      <strong>{narration.synthetic_voice ? 'Synthetic (AI-generated) narration' : 'Your own recording'}</strong>
      <audio controls preload="metadata" src={asset(narration.file_url)} aria-label="Current narration audio" />
      <div className="badge-row">
        <LicenseBadge asset={narration} />
        {narration.voice?.voice_id && <span className="muted">Voice {narration.voice.voice_id}</span>}
        <span className="muted">Timing: {narration.word_timing}</span>
      </div>
      {narration.synthetic_voice && (
        <p className="muted">Disclose synthetic voice when you publish. The rendered video carries a disclosure line.</p>
      )}
    </div>
  );
}

function UploadForm({ script, onChanged }) {
  const [file, setFile] = useState(null);
  const [recorded, setRecorded] = useState(false);
  const [invalid, setInvalid] = useState('');
  const { busy, error, run } = useAction();

  async function submit(event) {
    event.preventDefault();
    if (!file) return setInvalid('Choose an audio file.');
    if (!recorded) return setInvalid('Confirm that you recorded this narration yourself.');
    setInvalid('');
    const body = new FormData();
    body.append('file', file);
    body.append('i_recorded_this', 'true');
    body.append('title', `Narration: ${script.title || 'script'}`.slice(0, 300));
    const done = await run(async () => {
      const uploaded = await uploadNarration(body);
      return attachNarration(script.id, uploaded.id);
    });
    if (done) {
      setFile(null);
      setRecorded(false);
      onChanged();
    }
    return undefined;
  }

  return (
    <form className="upload-narration" onSubmit={submit} aria-label="Upload my own narration">
      <h3 className="sub-title">Upload my own narration</h3>
      <p className="muted">This always works, with or without a voice provider. Word timing is approximate for recordings.</p>
      <Field label="Narration audio file"><input type="file" accept="audio/*" onChange={e => setFile(e.target.files?.[0] || null)} /></Field>
      <label className="check">
        <input type="checkbox" checked={recorded} onChange={e => setRecorded(e.target.checked)} />
        <span>I recorded this myself.</span>
      </label>
      {invalid && <p className="field-error" role="alert">{invalid}</p>}
      <ErrorNote error={error} />
      <Button type="submit" busy={busy}>Upload narration</Button>
    </form>
  );
}

/** Step 5: voices, preview, generate or upload narration for one approved script. */
export default function VoicePanel({ project, script, busyJob, onChanged }) {
  const [voices, setVoices] = useState(null);
  const [loadError, setLoadError] = useState('');
  const [choice, setChoice] = useState('');
  const [speed, setSpeed] = useState(1);
  const [preview, setPreview] = useState(null);
  const previewAction = useAction();
  const generate = useAction();

  useEffect(() => {
    let current = true;
    setVoices(null);
    setLoadError('');
    listVoices(script.language || project.language)
      .then(data => { if (current) setVoices(data || { providers: [] }); })
      .catch(e => { if (current) setLoadError(e.message); });
    return () => { current = false; };
  }, [script.language, project.language]);

  const providers = useMemo(() => voices?.providers || [], [voices]);
  const options = useMemo(() => providers
    .filter(p => p.available)
    .flatMap(p => (p.voices || []).map(v => ({ key: `${p.name}::${v.id}`, provider: p.name, id: v.id, text: `${v.label || v.id} · ${p.label}` }))), [providers]);
  const selected = options.find(o => o.key === choice) || options[0];
  const noHosted = voices && !options.length;

  async function doPreview() {
    setPreview(null);
    const made = await previewAction.run(() => previewVoice({
      language: script.language || project.language, voice_id: selected.id, speed, provider: selected.provider,
    }));
    if (made) setPreview(made);
  }

  async function doGenerate() {
    const done = await generate.run(() => narrateScript(script.id, { provider: selected.provider, voice_id: selected.id, speed }));
    if (done) onChanged();
  }

  return (
    <section className="panel" aria-labelledby="voice-title">
      <h2 className="panel-title" id="voice-title">Narration</h2>
      <p className="muted">Voices generated here are synthetic (AI-generated). Preview before you generate.</p>
      <CurrentNarration narration={script.narration} />

      {!voices && !loadError && <p className="muted" aria-live="polite">Loading voices</p>}
      {loadError && <p className="story-error" role="alert">{loadError}</p>}

      {voices && (
        <>
          <ul className="provider-list" aria-label="Voice providers">
            {providers.map(p => <ProviderCard key={p.name} provider={p} />)}
          </ul>
          {noHosted ? (
            <div className="story-callout" role="status">
              <strong>No hosted voice provider is available right now.</strong>
              <p>The reasons are listed above. You can still narrate this script: upload your own recording below.</p>
            </div>
          ) : (
            <div className="voice-controls">
              <Field label="Voice">
                <select value={selected?.key || ''} onChange={e => setChoice(e.target.value)}>
                  {options.map(o => <option key={o.key} value={o.key}>{o.text}</option>)}
                </select>
              </Field>
              <Field label={`Speed ${speed.toFixed(2)}x`}>
                <input type="range" min="0.8" max="1.25" step="0.05" value={speed} onChange={e => setSpeed(Number(e.target.value))} />
              </Field>
              <div className="form-actions">
                <Button busy={previewAction.busy} disabled={busyJob} onClick={doPreview}>Preview voice</Button>
                <Button variant="primary" busy={generate.busy} disabled={busyJob} onClick={doGenerate}>Generate narration</Button>
              </div>
              <ErrorNote error={previewAction.error} />
              <ErrorNote error={generate.error} />
              {preview && (
                <div>
                  <p className="muted">Synthetic voice preview</p>
                  <audio controls src={asset(preview.file_url)} aria-label="Voice preview" />
                </div>
              )}
            </div>
          )}
        </>
      )}
      <UploadForm script={script} onChanged={onChanged} />
    </section>
  );
}
