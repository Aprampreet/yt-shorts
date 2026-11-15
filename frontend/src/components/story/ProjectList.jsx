import { useState } from 'react';
import { Link } from 'react-router-dom';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import EmptyState from '../ui/EmptyState';
import ErrorNote from './ErrorNote';
import useAction from './useAction';
import { CONTENT_FORMATS } from './constants';
import { deleteProject } from '../../api/story';

const STATUS_TONE = { draft: 'neutral', completed: 'success', needs_review: 'warn', failed: 'danger' };
const formatLabel = value => CONTENT_FORMATS.find(f => f.value === value)?.label || '';

function ProjectRow({ project, onDeleted }) {
  const [confirming, setConfirming] = useState(false);
  const { busy, error, run } = useAction();
  const counts = project.counts || {};

  async function remove() {
    const done = await run(async () => {
      await deleteProject(project.id);
      return true;
    });
    if (done) onDeleted();
  }

  return (
    <li className="story-project">
      <div className="story-project-main">
        <Link to={`/story/${project.id}`}><strong>{project.title}</strong></Link>
        <div className="badge-row">
          <Badge tone={STATUS_TONE[project.status] || 'neutral'}>{String(project.status || 'draft').replace(/_/g, ' ')}</Badge>
          {formatLabel(project.content_format) && <span className="muted">{formatLabel(project.content_format)}</span>}
        </div>
        <p className="muted">
          {counts.concepts || 0} concepts · {counts.scripts || 0} scripts · {counts.videos || 0} videos
        </p>
        <ErrorNote error={error} />
      </div>
      <div className="story-project-actions">
        <Link className="button secondary small" to={`/story/${project.id}`}>Open</Link>
        {confirming ? (
          <>
            <Button size="sm" variant="danger" busy={busy} onClick={remove}>Confirm delete</Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Keep</Button>
          </>
        ) : (
          <Button size="sm" variant="ghost" onClick={() => setConfirming(true)} aria-label={`Delete ${project.title}`}>Delete</Button>
        )}
      </div>
    </li>
  );
}

/** Project list for `/story`. Deleting removes the project, its scripts and rendered videos. */
export default function ProjectList({ projects, state, error, onRetry, onNew, onChanged }) {
  return (
    <section className="panel" aria-labelledby="story-projects-title">
      <div className="story-list-head">
        <h2 className="panel-title" id="story-projects-title">Your story projects</h2>
        <Button variant="primary" onClick={onNew}>New project</Button>
      </div>
      <div aria-live="polite">
        {state === 'loading' && <p className="muted">Loading projects</p>}
      </div>
      {state === 'error' && (
        <div className="story-error" role="alert">
          <p>{error}</p>
          <Button size="sm" onClick={onRetry}>Retry</Button>
        </div>
      )}
      {state === 'ready' && projects.length === 0 && (
        <EmptyState title="No story projects yet.">Create one from a transcript, notes or a video you have rights to.</EmptyState>
      )}
      {state === 'ready' && projects.length > 0 && (
        <ul className="story-project-list" aria-label="Story projects">
          {projects.map(project => <ProjectRow key={project.id} project={project} onDeleted={onChanged} />)}
        </ul>
      )}
    </section>
  );
}
