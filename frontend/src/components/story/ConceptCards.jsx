import { useEffect, useState } from 'react';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import ErrorNote from './ErrorNote';
import useAction from './useAction';
import { generateConcepts, selectConcepts } from '../../api/story';

// Mirrors scoring.MIN_CONCEPT_DISTANCE; the project detail does not expose it.
const DIVERSITY_THRESHOLD = 0.35;
const ANGLE_LABELS = {
  explainer: 'Explainer', timeline: 'Timeline', myth_vs_fact: 'Myth vs fact', top_list: 'Top list',
  contrarian: 'Contrarian', comparison: 'Comparison', deep_dive: 'Deep dive', story: 'Story',
};
const fixed = value => (Number.isFinite(Number(value)) ? Number(value).toFixed(2) : 'n/a');

/** Step 3: concept cards. Scores are internal editorial heuristics, not predictions of views. */
export default function ConceptCards({ project, busyJob, onStart, onChanged }) {
  const concepts = project.concepts || [];
  const limit = project.video_count || 3;
  const [picked, setPicked] = useState(() => concepts.filter(c => c.selected).map(c => c.id));
  const generate = useAction();
  const save = useAction();

  useEffect(() => {
    setPicked(concepts.filter(c => c.selected).map(c => c.id));
  }, [project.concepts]); // eslint-disable-line react-hooks/exhaustive-deps

  const atLimit = picked.length >= limit;
  const toggle = id => setPicked(old => (old.includes(id) ? old.filter(x => x !== id) : old.length >= limit ? old : [...old, id]));
  const noProvider = project.text_provider && !project.text_provider.available;
  const lowDiversity = concepts.length > 1 && concepts.some(c => Number(c.diversity_score) < DIVERSITY_THRESHOLD);

  async function saveSelection() {
    const done = await save.run(() => selectConcepts(project.id, picked));
    if (done) onChanged();
  }

  return (
    <section className="panel" aria-labelledby="concepts-title">
      <h2 className="panel-title" id="concepts-title">Concepts</h2>
      <p className="muted">
        Each concept is a different angle on the same facts. Quality and diversity are internal editorial heuristics
        (structure, grounding, overlap between concepts). They are not predictions of views or virality.
      </p>
      {noProvider && (
        <div className="story-callout" role="status">
          <strong>AI concept writing is unavailable.</strong>
          <ul>{(project.text_provider.reasons || []).map(r => <li key={r}>{r}</li>)}</ul>
          <p>You can still write your own script in the next step.</p>
        </div>
      )}
      <div className="form-actions">
        <Button
          variant="primary"
          busy={generate.busy}
          disabled={busyJob || noProvider}
          onClick={() => generate.run(() => onStart(() => generateConcepts(project.id)))}
        >
          {concepts.length ? 'Generate new concepts' : 'Generate concepts'}
        </Button>
      </div>
      <ErrorNote error={generate.error} />

      {concepts.length > 0 && (
        <>
          {lowDiversity && (
            <p className="story-callout" role="status">
              Some concepts overlap: diversity is below {DIVERSITY_THRESHOLD.toFixed(2)}. Pick concepts with different angles.
            </p>
          )}
          <p className="muted" aria-live="polite">Selected {picked.length} of {limit}</p>
          <ul className="concept-grid" aria-label="Concepts">
            {concepts.map(concept => {
              const checked = picked.includes(concept.id);
              return (
                <li key={concept.id} className={`concept${checked ? ' selected' : ''}`}>
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!checked && atLimit}
                      onChange={() => toggle(concept.id)}
                      aria-label={`Select concept: ${concept.title}`}
                    />
                    <span><strong>{concept.title}</strong></span>
                  </label>
                  <div className="badge-row">
                    <Badge tone="accent">{ANGLE_LABELS[concept.angle] || concept.angle}</Badge>
                    {concept.has_script && <Badge>Has script</Badge>}
                  </div>
                  <p>{concept.logline}</p>
                  <p className="muted">Hook idea: {concept.hook_idea}</p>
                  <ul className="plain-list">{(concept.key_points || []).map(point => <li key={point}>{point}</li>)}</ul>
                  <p className="muted">
                    Quality (heuristic) {fixed(concept.quality_score)} · Diversity (heuristic) {fixed(concept.diversity_score)}
                  </p>
                </li>
              );
            })}
          </ul>
          {atLimit && <p className="muted">You reached the limit of {limit}. Deselect one to choose another.</p>}
          <ErrorNote error={save.error} />
          <div className="form-actions">
            <Button variant="primary" busy={save.busy} disabled={!picked.length} onClick={saveSelection}>Save selection</Button>
          </div>
        </>
      )}
    </section>
  );
}
