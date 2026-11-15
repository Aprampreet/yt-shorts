import { useState } from 'react';
import Button from '../ui/Button';
import Field from '../ui/Field';
import ErrorNote from './ErrorNote';
import useAction from './useAction';
import { CONTENT_FORMATS, LANGUAGES, NARRATION_STYLES, TARGET_DURATIONS } from './constants';
import { createProject } from '../../api/story';
import { TRANSCRIPT_LIMIT, validateInput } from './inputRules';

const INITIAL = {
  url: '',
  transcript: '',
  title: '',
  language: 'en',
  content_format: 'gaming_commentary',
  narration_style: 'natural',
  target_duration: '30_45',
  video_count: 3,
  rights_declaration: false,
};

function Select({ options, ...rest }) {
  return (
    <select {...rest}>
      {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

/**
 * Step 1. Creates the project, then hands it to the caller (which navigates to it).
 * A public video is not free to reuse, so a URL needs an explicit rights declaration.
 */
export default function StoryInputForm({ onCreated, onCancel }) {
  const [form, setForm] = useState(INITIAL);
  const [errors, setErrors] = useState({});
  const { busy, error, run } = useAction();
  const set = (key, value) => setForm(old => ({ ...old, [key]: value }));
  const hasUrl = Boolean(form.url.trim());

  async function submit(event) {
    event.preventDefault();
    const found = validateInput(form);
    setErrors(found);
    if (Object.keys(found).length) return;
    const url = form.url.trim();
    const payload = {
      url: url || null,
      transcript: form.transcript.trim() || null,
      title: form.title.trim(),
      language: form.language,
      content_format: form.content_format,
      narration_style: form.narration_style,
      target_duration: form.target_duration,
      video_count: Number(form.video_count),
      rights_declaration: url ? form.rights_declaration : false,
    };
    const project = await run(() => createProject(payload));
    if (project) onCreated(project);
  }

  return (
    <form className="panel story-form" onSubmit={submit} aria-label="New story project" noValidate>
      <h2 className="panel-title">New story project</h2>
      <p className="muted">
        Start from a video you have rights to, your own notes, or both. Everything the studio writes is original
        commentary; nothing is fact-checked for you.
      </p>
      {errors.source && <p className="field-error" role="alert">{errors.source}</p>}

      <Field label="YouTube URL (optional)" hint="Used as the source reference. Public does not mean free to reuse.">
        <input type="url" inputMode="url" value={form.url} onChange={e => set('url', e.target.value)} placeholder="https://www.youtube.com/watch?v=" />
      </Field>

      <Field label="Transcript or notes" error={errors.transcript}>
        <textarea
          className="story-transcript"
          rows={9}
          value={form.transcript}
          onChange={e => set('transcript', e.target.value)}
          placeholder="Paste a transcript, or write the facts you want the story built on."
        />
      </Field>
      <div className={`counter${form.transcript.length > TRANSCRIPT_LIMIT ? ' over' : ''}`} aria-live="polite">
        {form.transcript.length.toLocaleString()} / {TRANSCRIPT_LIMIT.toLocaleString()} characters
      </div>

      {hasUrl && (
        <div className="story-rights">
          <label className="check">
            <input
              type="checkbox"
              checked={form.rights_declaration}
              onChange={e => set('rights_declaration', e.target.checked)}
              aria-describedby="story-rights-help"
            />
            <span>I own this video or have permission to use it.</span>
          </label>
          <p id="story-rights-help" className="muted">
            A video being public on YouTube does not make it free to reuse. Without ownership or permission, use only
            your own notes.
          </p>
          {errors.rights && <p className="field-error" role="alert">{errors.rights}</p>}
        </div>
      )}

      <div className="form-grid">
        <Field label="Title (optional)" className="wide">
          <input value={form.title} maxLength={300} onChange={e => set('title', e.target.value)} />
        </Field>
        <Field label="Language">
          <Select options={LANGUAGES} value={form.language} onChange={e => set('language', e.target.value)} />
        </Field>
        <Field label="Content format">
          <Select options={CONTENT_FORMATS} value={form.content_format} onChange={e => set('content_format', e.target.value)} />
        </Field>
        <Field label="Narration style">
          <Select options={NARRATION_STYLES} value={form.narration_style} onChange={e => set('narration_style', e.target.value)} />
        </Field>
        <Field label="Target duration">
          <Select options={TARGET_DURATIONS} value={form.target_duration} onChange={e => set('target_duration', e.target.value)} />
        </Field>
        <Field label="Number of videos">
          <Select
            options={[1, 2, 3].map(n => ({ value: n, label: String(n) }))}
            value={form.video_count}
            onChange={e => set('video_count', e.target.value)}
          />
        </Field>
      </div>

      <ErrorNote error={error} />
      <div className="form-actions">
        <Button type="submit" variant="primary" busy={busy}>{busy ? 'Creating project' : 'Create project'}</Button>
        {onCancel && <Button variant="ghost" onClick={onCancel}>Cancel</Button>}
      </div>
    </form>
  );
}
