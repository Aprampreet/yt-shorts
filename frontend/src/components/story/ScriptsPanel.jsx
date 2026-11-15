import { useState } from 'react';
import Button from '../ui/Button';
import Field from '../ui/Field';
import ErrorNote from './ErrorNote';
import ScriptEditor from './ScriptEditor';
import useAction from './useAction';
import { LANGUAGES } from './constants';
import { createManualScript, generateScripts } from '../../api/story';

function ManualScriptForm({ project, onCreated, prominent }) {
  const [title, setTitle] = useState('');
  const [text, setText] = useState('');
  const [language, setLanguage] = useState(project.language || 'en');
  const [invalid, setInvalid] = useState('');
  const { busy, error, run } = useAction();

  async function submit(event) {
    event.preventDefault();
    if (!title.trim() || !text.trim()) {
      setInvalid('A title and the script text are both required.');
      return;
    }
    setInvalid('');
    const script = await run(() => createManualScript(project.id, { title: title.trim(), language, full_text: text.trim() }));
    if (script) {
      setTitle('');
      setText('');
      onCreated(script);
    }
  }

  return (
    <form className="manual-script" onSubmit={submit} aria-label="Write my own script">
      <h3 className="sub-title">Write my own script</h3>
      <p className="muted">
        {prominent
          ? 'This is the main path while no AI text provider is available. '
          : ''}
        Original writing only. The studio splits it into hook, context, core points and payoff; you can refine each part
        after it is created. It is scored like any other script.
      </p>
      <Field label="Script title"><input value={title} maxLength={120} onChange={e => setTitle(e.target.value)} /></Field>
      <Field label="Script language">
        <select value={language} onChange={e => setLanguage(e.target.value)}>
          {LANGUAGES.map(l => <option key={l.value} value={l.value}>{l.label}</option>)}
        </select>
      </Field>
      <Field label="Script text" hint="Up to 6,000 characters.">
        <textarea rows={7} maxLength={6000} value={text} onChange={e => setText(e.target.value)} />
      </Field>
      {invalid && <p className="field-error" role="alert">{invalid}</p>}
      <ErrorNote error={error} />
      <Button type="submit" variant={prominent ? 'primary' : 'secondary'} busy={busy}>Create script</Button>
    </form>
  );
}

/** Step 4: AI scripts for the selected concepts, plus an always-available manual path. */
export default function ScriptsPanel({ project, busyJob, onStart, onChanged }) {
  const scripts = project.scripts || [];
  const hasSelected = (project.concepts || []).some(c => c.selected);
  const provider = project.text_provider || { available: true, reasons: [] };
  const generate = useAction();
  const waiting = (project.concepts || []).filter(c => c.selected && !c.has_script).length;

  return (
    <section className="panel" aria-labelledby="scripts-title">
      <h2 className="panel-title" id="scripts-title">Scripts</h2>
      <p className="muted">
        Scripts are original narration. Scores are editorial heuristics, not a prediction of views. Approve a script to
        unlock narration.
      </p>

      {!provider.available && (
        <div className="story-callout" role="status">
          <strong>AI script writing is unavailable.</strong>
          <ul>{(provider.reasons || []).map(r => <li key={r}>{r}</li>)}</ul>
          <p>Write your own script below. Narration, storyboard and rendering work the same way.</p>
        </div>
      )}

      {provider.available && (
        <div className="form-actions">
          <Button
            variant="primary"
            busy={generate.busy}
            disabled={busyJob || !hasSelected}
            onClick={() => generate.run(() => onStart(() => generateScripts(project.id, scripts.length > 0 && waiting === 0)))}
          >
            Write scripts
          </Button>
          {!hasSelected && <span className="muted">Select concepts first, or write your own script.</span>}
        </div>
      )}
      <ErrorNote error={generate.error} />

      {!provider.available && <ManualScriptForm project={project} onCreated={() => onChanged()} prominent />}

      {scripts.length === 0 && <p className="muted">No scripts yet.</p>}
      {scripts.map(script => (
        <ScriptEditor
          key={`${script.id}:${script.version}:${script.full_text}`}
          script={script}
          project={project}
          busyJob={busyJob}
          onStart={onStart}
          onChanged={() => onChanged()}
        />
      ))}

      {provider.available && (
        <details>
          <summary>Write my own script</summary>
          <ManualScriptForm project={project} onCreated={() => onChanged()} />
        </details>
      )}
    </section>
  );
}
