import Button from '../ui/Button';
import Spinner from '../ui/Spinner';
import StatusPill from '../ui/StatusPill';
import ErrorNote from './ErrorNote';
import { JOB_LABELS, jobKind } from './constants';

/**
 * Status of the running story job. Progress is whatever GET /jobs/{id} reports.
 * Announced politely to screen readers.
 */
export default function JobStrip({ job, onCancel, cancelling, cancelError }) {
  if (!job) return null;
  return (
    <section className="job-strip" aria-label="Running step">
      <div className="job-strip-head" aria-live="polite">
        <Spinner />
        <strong>{JOB_LABELS[jobKind(job)] || 'Working'}</strong>
        <StatusPill phase={job.phase || job.status} />
        <span className="muted">{job.stage}</span>
      </div>
      <progress aria-label="Step progress" value={job.progress || 0} max="100" />
      <div className="form-actions">
        <span className="muted">{Math.round(job.progress || 0)}%</span>
        <Button size="sm" variant="danger" busy={cancelling} onClick={onCancel}>Cancel step</Button>
      </div>
      <ErrorNote error={cancelError} />
    </section>
  );
}
