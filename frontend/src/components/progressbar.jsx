const stages = ['Source', 'Analysis', 'Selection', 'Editing', 'Quality check'];
export default function ProgressBar({ job, onCancel }) {
  return <section className="processing" aria-live="polite"><div className="section-heading"><div><span className="eyebrow">IN THE EDITING ROOM</span><h2>{job.stage}</h2></div><strong>{job.progress}%</strong></div>
    <progress aria-label="Generation progress" value={job.progress} max="100" />
    <div className="stage-list">{stages.map((s, i) => <span key={s} className={job.progress >= [4,12,42,44,90][i] ? 'active' : ''}><i />{s}</span>)}</div>
    <div className="processing-footer"><p>Progress reflects worker stages. Encoding time depends on the source and hardware.</p><button onClick={onCancel}>Cancel generation</button></div>
  </section>;
}
