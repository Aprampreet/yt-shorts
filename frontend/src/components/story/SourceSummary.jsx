import Badge from '../ui/Badge';
import { CONTENT_FORMATS, LANGUAGES, NARRATION_STYLES, TARGET_DURATIONS } from './constants';

const label = (list, value) => list.find(item => item.value === value)?.label || value || 'Not set';

/** Step 1 once the project exists: what it was created from. Inputs are fixed after creation. */
export default function SourceSummary({ project }) {
  const inputs = project.inputs || {};
  return (
    <section className="panel" aria-labelledby="source-title">
      <h2 className="panel-title" id="source-title">Source and settings</h2>
      <dl className="meta-list">
        <div><dt>YouTube URL</dt><dd>{project.source?.url || 'None'}</dd></div>
        <div><dt>Transcript or notes</dt><dd>{project.transcript_chars ? `${project.transcript_chars.toLocaleString()} characters` : 'None'}</dd></div>
        <div><dt>Language</dt><dd>{label(LANGUAGES, project.language)}</dd></div>
        <div><dt>Content format</dt><dd>{label(CONTENT_FORMATS, inputs.content_format)}</dd></div>
        <div><dt>Narration style</dt><dd>{label(NARRATION_STYLES, inputs.narration_style)}</dd></div>
        <div><dt>Target duration</dt><dd>{label(TARGET_DURATIONS, inputs.target_duration)}</dd></div>
        <div><dt>Number of videos</dt><dd>{inputs.video_count}</dd></div>
      </dl>
      {project.source?.url && (
        <p className="muted">
          <Badge tone={project.source.rights_declaration ? 'success' : 'warn'}>
            {project.source.rights_declaration ? 'Rights declaration on file' : 'No rights declaration'}
          </Badge>{' '}
          A public video is not free to reuse; the declaration records your own statement and verifies nothing.
        </p>
      )}
    </section>
  );
}
