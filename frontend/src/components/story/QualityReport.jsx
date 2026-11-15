import Badge from '../ui/Badge';
import { COMPONENT_LABELS } from './constants';

const pct = value => `${Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100)}%`;
const fixed = value => (Number.isFinite(Number(value)) ? Number(value).toFixed(2) : 'n/a');

/**
 * Script quality report from the backend. Scores are deterministic editorial heuristics
 * (structure, length, grounding, originality, hook, readability), not a prediction of views.
 */
export default function QualityReport({ report }) {
  if (!report || report.score === undefined) {
    return <p className="muted">No quality report yet.</p>;
  }
  const originality = report.originality || {};
  const gates = report.gates || {};
  return (
    <section className="quality" aria-label="Quality report">
      <div className="quality-head">
        <strong className="quality-score">{fixed(report.score)}</strong>
        <span className="muted">of 1.00 · threshold {fixed(report.threshold)}</span>
        <Badge tone={report.passed ? 'success' : 'warn'}>{report.passed ? 'Passes' : 'Below threshold'}</Badge>
        {gates.originality === false && <Badge tone="danger">Too close to source</Badge>}
      </div>
      <ul className="quality-bars">
        {Object.entries(report.components || {}).map(([key, value]) => (
          <li key={key}>
            <div className="quality-bar-label">
              <span>{COMPONENT_LABELS[key] || key}</span>
              <span>{fixed(value)}</span>
            </div>
            <div className="bar" role="img" aria-label={`${COMPONENT_LABELS[key] || key} ${pct(value)}`}>
              <span style={{ width: pct(value) }} />
            </div>
            {report.explanations?.[key] && <small className="muted">{report.explanations[key]}</small>}
          </li>
        ))}
      </ul>
      {(report.reasons || []).length > 0 && (
        <>
          <h4 className="sub-title">Why it may fall short</h4>
          <ul className="plain-list">{report.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
        </>
      )}
      {originality.containment_5 !== undefined && (
        <p className="muted">
          Originality vs source: 5-gram overlap {fixed(originality.containment_5)}, longest copied run {originality.longest_run ?? 0} words.
        </p>
      )}
      {report.basis && <p className="muted">{report.basis}</p>}
    </section>
  );
}
