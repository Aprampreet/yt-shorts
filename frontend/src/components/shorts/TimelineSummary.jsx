import Badge from '../ui/Badge';

const QA = {
  passed: ['QA passed', 'success'],
  flagged: ['QA flagged', 'warn'],
  failed: ['QA failed', 'danger'],
};

const label = value => String(value).replace(/_/g, ' ');

/**
 * Edit summary for a Short from `timeline_summary` and `qa_status`. Every key
 * is optional; whatever is absent is simply not shown, and nothing renders
 * when there is nothing to show.
 */
export default function TimelineSummary({ summary, qaStatus }) {
  const rows = [];
  if (summary && typeof summary === 'object') {
    if (summary.preset && summary.preset !== 'none') rows.push(['Preset', label(summary.preset)]);
    if (summary.composition) rows.push(['Composition', label(summary.composition)]);
    if (summary.reframe_label) rows.push(['Framing', summary.reframe_label]);
    if (summary.caption_renderer) rows.push(['Captions', `${summary.caption_renderer} renderer`]);
    if (Number.isFinite(summary.removed_seconds)) rows.push(['Dead air removed', `${summary.removed_seconds.toFixed(1)}s`]);
    if (summary.punch_ins !== undefined && summary.punch_ins !== null) {
      rows.push(['Punch-ins', typeof summary.punch_ins === 'number' ? String(summary.punch_ins) : summary.punch_ins ? 'on' : 'off']);
    }
    if (summary.hook_overlay?.text) {
      rows.push(['Hook', `“${summary.hook_overlay.text}”${summary.hook_overlay.source ? ` (${label(summary.hook_overlay.source)})` : ''}`]);
    }
  }
  const qa = QA[qaStatus];
  if (!rows.length && !qa) return null;
  return (
    <section className="timeline-summary" aria-label="Edit summary">
      {qa && <Badge tone={qa[1]}>{qa[0]}</Badge>}
      {rows.length > 0 && (
        <dl>
          {rows.map(([name, value]) => (
            <div key={name}><dt>{name}</dt><dd>{value}</dd></div>
          ))}
        </dl>
      )}
    </section>
  );
}
