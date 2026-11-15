import { useState } from 'react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Field from '../ui/Field';
import ErrorNote from './ErrorNote';
import QualityReport from './QualityReport';
import useAction from './useAction';
import { SCRIPT_SECTIONS, formatSeconds } from './constants';
import { approveScript, editScript, regenerateScript } from '../../api/story';

const fromScript = script => ({
  title: script.title || '',
  hook: script.sections?.hook || '',
  context: script.sections?.context || '',
  core: script.sections?.core?.length ? script.sections.core : [''],
  payoff: script.sections?.payoff || '',
  cta: script.sections?.cta || '',
});

/** One script: sectioned editor, quality report, save (re-scored by the backend), regenerate and approve. */
export default function ScriptEditor({ script, project, busyJob, onStart, onChanged }) {
  const [draft, setDraft] = useState(() => fromScript(script));
  const [confirming, setConfirming] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const save = useAction();
  const approve = useAction();
  const regen = useAction();
  const set = (key, value) => setDraft(old => ({ ...old, [key]: value }));
  const setCore = (index, value) => setDraft(old => ({ ...old, core: old.core.map((c, i) => (i === index ? value : c)) }));

  const saved = fromScript(script);
  const dirty = JSON.stringify(saved) !== JSON.stringify(draft);
  const report = script.quality_report || {};
  const originalityFailed = (report.gates || {}).originality === false;
  const approved = script.status === 'approved';
  const noProvider = project.text_provider && !project.text_provider.available;

  async function saveEdits() {
    const core = draft.core.map(c => c.trim()).filter(Boolean);
    const updated = await save.run(() => editScript(script.id, {
      title: draft.title.trim(),
      sections: { hook: draft.hook.trim(), context: draft.context.trim(), core, payoff: draft.payoff.trim(), cta: draft.cta.trim() },
    }));
    if (updated) onChanged(updated);
  }

  async function doApprove(override) {
    const updated = await approve.run(() => approveScript(script.id, override));
    if (updated) {
      setConfirming(false);
      setAcknowledged(false);
      onChanged(updated);
    }
  }

  function startApprove() {
    if (report.passed) doApprove(false);
    else setConfirming(true);
  }

  return (
    <article className="script-card" aria-label={`Script: ${script.title || 'Untitled'}`}>
      <div className="script-head">
        <h3>{script.title || 'Untitled script'}</h3>
        <div className="badge-row">
          <Badge tone={approved ? 'success' : 'neutral'}>{approved ? 'Approved' : 'Draft'}</Badge>
          <Badge>{script.manual ? 'Written by you' : 'AI draft'}</Badge>
          <span className="muted">{script.word_count} words · about {formatSeconds(script.estimated_duration)}</span>
        </div>
      </div>

      <Field label="Title"><input value={draft.title} maxLength={120} onChange={e => set('title', e.target.value)} /></Field>
      <Field label="Hook"><textarea rows={2} value={draft.hook} maxLength={400} onChange={e => set('hook', e.target.value)} /></Field>
      <Field label="Context"><textarea rows={3} value={draft.context} maxLength={1200} onChange={e => set('context', e.target.value)} /></Field>
      <fieldset className="core-points">
        <legend>Core points</legend>
        {draft.core.map((point, index) => (
          <div className="core-row" key={index}>
            <Field label={`Core point ${index + 1}`}>
              <textarea rows={2} value={point} maxLength={1500} onChange={e => setCore(index, e.target.value)} />
            </Field>
            {draft.core.length > 1 && (
              <Button size="sm" variant="ghost" onClick={() => set('core', draft.core.filter((_, i) => i !== index))}
                aria-label={`Remove core point ${index + 1}`}>Remove</Button>
            )}
          </div>
        ))}
        {draft.core.length < 6 && <Button size="sm" onClick={() => set('core', [...draft.core, ''])}>Add core point</Button>}
      </fieldset>
      <Field label="Payoff"><textarea rows={2} value={draft.payoff} maxLength={800} onChange={e => set('payoff', e.target.value)} /></Field>
      <Field label={SCRIPT_SECTIONS[3].label}><textarea rows={2} value={draft.cta} maxLength={300} onChange={e => set('cta', e.target.value)} /></Field>

      <ErrorNote error={save.error} />
      <div className="form-actions">
        <Button variant="primary" busy={save.busy} disabled={!dirty} onClick={saveEdits}>Save edits</Button>
        {dirty && <span className="muted">Unsaved edits. Saving re-scores the script and clears any narration or storyboard built from the old text.</span>}
      </div>

      <QualityReport report={script.quality_report} />

      <ErrorNote error={regen.error} />
      <div className="form-actions">
        {!script.manual && (
          <Button
            busy={regen.busy}
            disabled={busyJob || noProvider || !script.attempts_left}
            onClick={() => regen.run(() => onStart(() => regenerateScript(script.id)))}
          >
            Regenerate ({script.attempts_left ?? 0} left)
          </Button>
        )}
        {!approved && (
          <Button variant="primary" busy={approve.busy} disabled={dirty || originalityFailed} onClick={startApprove}>
            Approve script
          </Button>
        )}
      </div>
      {dirty && !approved && <p className="muted">Save your edits before approving.</p>}
      {originalityFailed && (
        <p className="story-error" role="alert">
          This script is too close to the source text, so it cannot be approved, even by override. Rewrite it in your own
          words, then save.
        </p>
      )}
      {confirming && !approved && !originalityFailed && (
        <div className="story-callout" role="group" aria-label="Approve despite low score">
          <p>
            This script scored {Number(report.score || 0).toFixed(2)}, below the {Number(report.threshold || 0).toFixed(2)} threshold.
          </p>
          <label className="check">
            <input type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} />
            <span>I reviewed the quality report and want to approve this script anyway.</span>
          </label>
          <div className="form-actions">
            <Button variant="danger" busy={approve.busy} disabled={!acknowledged} onClick={() => doApprove(true)}>
              Approve despite low score
            </Button>
            <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
          </div>
        </div>
      )}
      <ErrorNote error={approve.error} />
    </article>
  );
}
