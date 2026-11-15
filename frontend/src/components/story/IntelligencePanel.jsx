import { useState } from 'react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Field from '../ui/Field';
import ErrorNote from './ErrorNote';
import useAction from './useAction';
import { CLAIM_STATUS } from './constants';
import { analyzeProject, attachEvidence, removeEvidence } from '../../api/story';

function EvidenceForm({ projectId, claim, onChanged }) {
  const [url, setUrl] = useState('');
  const [note, setNote] = useState('');
  const [invalid, setInvalid] = useState('');
  const { busy, error, run } = useAction();

  async function submit(event) {
    event.preventDefault();
    if (!url.trim() && !note.trim()) {
      setInvalid('Add a link or a note as evidence.');
      return;
    }
    setInvalid('');
    const saved = await run(() => attachEvidence(projectId, claim.id, { evidence_url: url.trim(), evidence_note: note.trim() }));
    if (saved) {
      setUrl('');
      setNote('');
      onChanged();
    }
  }

  return (
    <form className="evidence-form" onSubmit={submit} aria-label={`Add evidence for claim ${claim.id}`}>
      <Field label="Evidence link"><input type="url" value={url} onChange={e => setUrl(e.target.value)} placeholder="https://" /></Field>
      <Field label="Evidence note"><textarea rows={2} value={note} onChange={e => setNote(e.target.value)} /></Field>
      {invalid && <p className="field-error" role="alert">{invalid}</p>}
      <ErrorNote error={error} />
      <Button type="submit" size="sm" busy={busy}>Save evidence</Button>
    </form>
  );
}

function Claim({ projectId, claim, onChanged }) {
  const status = CLAIM_STATUS[claim.status] || { label: claim.status, tone: 'neutral' };
  const { busy, error, run } = useAction();
  const evidence = claim.evidence;

  async function remove() {
    const done = await run(() => removeEvidence(projectId, claim.id));
    if (done) onChanged();
  }

  return (
    <li className="claim">
      <div className="badge-row">
        <Badge tone={status.tone}>{status.label}</Badge>
        <span className="muted">{claim.id}</span>
      </div>
      <p>{claim.text}</p>
      {claim.quote && <blockquote>{claim.quote}</blockquote>}
      {evidence ? (
        <div className="claim-evidence">
          <p className="muted">Evidence you supplied. It is not checked automatically.</p>
          {evidence.url && <a href={evidence.url} target="_blank" rel="noreferrer noopener">{evidence.url}</a>}
          {evidence.note && <p>{evidence.note}</p>}
          <Button size="sm" variant="ghost" busy={busy} onClick={remove} aria-label={`Remove evidence from claim ${claim.id}`}>
            Remove evidence
          </Button>
        </div>
      ) : (
        <details>
          <summary>Add evidence</summary>
          <EvidenceForm projectId={projectId} claim={claim} onChanged={onChanged} />
        </details>
      )}
      <ErrorNote error={error} />
    </li>
  );
}

function methodSummary(intel) {
  if (intel.method === 'llm') return `AI analysis${intel.provider ? ` (${intel.provider})` : ''}`;
  if (intel.method === 'deterministic') return 'Deterministic fallback (no text provider)';
  return 'No usable text found';
}

/** Step 2: source intelligence. Claims are labelled by origin; none are independently fact-checked. */
export default function IntelligencePanel({ project, busyJob, onStart, onChanged }) {
  const intel = project.intelligence || {};
  const analyzed = Boolean(intel.method);
  const { busy, error, run } = useAction();
  const claims = intel.claims || [];

  return (
    <section className="panel" aria-labelledby="intel-title">
      <h2 className="panel-title" id="intel-title">Source intelligence</h2>
      <p className="muted">
        The studio reads your source and lists its claims. Nothing is independently fact-checked: a claim is only as
        reliable as the source, unless you attach your own evidence.
      </p>
      <div className="form-actions">
        <Button variant="primary" busy={busy} disabled={busyJob} onClick={() => run(() => onStart(() => analyzeProject(project.id)))}>
          {analyzed ? 'Analyze again' : 'Analyze source'}
        </Button>
      </div>
      <ErrorNote error={error} />

      {analyzed && (
        <>
          <div className="story-callout">
            <strong>Method: {methodSummary(intel)}</strong>
            {intel.text_provider_unavailable && (
              <p>
                No text provider is available, so claims were extracted by rules (sentences with numbers, dates or
                names) and nothing was summarized or inferred.
              </p>
            )}
            {(intel.provider_reasons || []).length > 0 && (
              <ul>{intel.provider_reasons.map(r => <li key={r}>{r}</li>)}</ul>
            )}
            {(intel.notes || []).map(note => <p key={note} className="muted">{note}</p>)}
          </div>

          {intel.summary && <p>{intel.summary}</p>}

          {(intel.sections || []).length > 0 && (
            <>
              <h3 className="sub-title">Sections</h3>
              <ol className="story-sections">
                {intel.sections.map(section => (
                  <li key={section.id}><strong>{section.title}</strong> <span className="muted">{section.units} lines</span></li>
                ))}
              </ol>
            </>
          )}

          <h3 className="sub-title">Claims ({claims.length})</h3>
          {claims.length === 0
            ? <p className="muted">No claims were found in this source.</p>
            : <ul className="claim-list" aria-label="Claims">{claims.map(c => <Claim key={c.id} projectId={project.id} claim={c} onChanged={onChanged} />)}</ul>}

          {(intel.unknowns || []).length > 0 && (
            <details>
              <summary>Unknowns ({intel.unknowns.length})</summary>
              <ul>{intel.unknowns.map(u => <li key={u}>{u}</li>)}</ul>
            </details>
          )}
          {(intel.duplicates || []).length > 0 && (
            <details>
              <summary>Removed duplicates ({intel.duplicates.length})</summary>
              <ul>{intel.duplicates.map(d => <li key={d.text}>{d.text}</li>)}</ul>
            </details>
          )}
        </>
      )}
    </section>
  );
}
