import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import InputCard from '../components/input_card';
import OutputCard from '../components/output_card';
import ProgressBar from '../components/progressbar';
import { api, asset } from '../components/api';
import { isTerminal } from '../hooks/useJobPolling';
import PageHeading from '../layout/PageHeading';

const WAVEFORM = [18, 30, 20, 45, 65, 35, 80, 50, 28, 63, 90, 40, 60, 32, 18, 48, 70, 37, 24, 15];

/**
 * Shorts Generator (route `/`). Behaviour is the original studio view; the
 * active job, history and errors are owned by the shell so they survive
 * route changes.
 */
export default function ShortsPage({ job, setJob, connected, loadHistory, setError, notices, onScheduled }) {
  const navigate = useNavigate();
  const [submitting, setSubmitting] = useState(false);
  const [scheduling, setScheduling] = useState(false);
  const busy = submitting || !!(job && !isTerminal(job.status));

  async function create(data) {
    setError('');
    setSubmitting(true);
    try {
      let body;
      let path = '/jobs';
      if (data.file) {
        body = new FormData();
        body.append('file', data.file);
        body.append('rights_declaration', String(data.rights_declaration));
        body.append('permission_reference', data.permission_reference);
        body.append('options', JSON.stringify(data.options));
        path = '/uploads';
      } else {
        body = JSON.stringify(data);
      }
      setJob(await api(path, { method: 'POST', body }));
      await loadHistory();
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  async function action(name) {
    setSubmitting(true);
    setError('');
    try {
      setJob(await api(`/jobs/${job.id}/${name}`, { method: 'POST' }));
      await loadHistory();
    } catch (e) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  function updateShort(short) {
    setJob(old => ({ ...old, results: old.results.map(s => (s.id === short.id ? short : s)) }));
  }

  async function scheduleAll() {
    if (!job || scheduling) return;
    setScheduling(true);
    setError('');
    onScheduled('');
    try {
      const result = await api(`/jobs/${job.id}/schedule-batch`, { method: 'POST' });
      onScheduled(
        result.already_scheduled
          ? 'These Shorts are already queued or uploaded on YouTube.'
          : result.already_uploaded
            ? 'Previously uploaded Shorts were kept. The remaining Shorts are queued at their planned slots.'
            : 'Upload started. Your Shorts are queued at 0, 5 and 10 hour slots where available.',
      );
      navigate('/publish');
    } catch (e) {
      setError(e.message);
    } finally {
      setScheduling(false);
    }
  }

  return (
    <>
      <PageHeading
        title="Good footage."
        subtitle="Three fresh perspectives."
        description="Turn your authorized footage into vertical Shorts and schedule them together."
      />
      {notices}

      <InputCard onSubmit={create} busy={busy} connected={connected} />

      {job && !isTerminal(job.status) && <ProgressBar job={job} onCancel={() => action('cancel')} />}

      {job?.status === 'failed' && (
        <section className="failure" role="alert">
          <h2>{job.stage}</h2>
          <p>{job.error_message}</p>
          <button disabled={busy} onClick={() => action('retry')}>Retry generation ↗</button>
        </section>
      )}

      {job?.status === 'cancelled' && (
        <p className="notice">Generation cancelled. The worker stops at the next processing boundary.</p>
      )}

      {job?.analysis?.warnings?.length > 0 && (
        <details className="warning-panel" open={isTerminal(job.status)}>
          <summary>Generation notes · {job.analysis.warnings.length}</summary>
          {job.analysis.warnings.map(w => <p key={w}>{w}</p>)}
        </details>
      )}

      {job?.results?.length > 0 ? (
        <section className="results">
          <div className="section-heading">
            <div>
              <span className="eyebrow">THE FINAL CUTS</span>
              <h2>{job.results.length} Shorts ready</h2>
            </div>
            <a className="button secondary" href={asset(`/api/v1/jobs/${job.id}/download`)}>↓ Download all</a>
          </div>

          <div className="schedule-all">
            <div>
              <strong>Publish in one click</strong>
              <p>
                {job.results
                  .map((_, index) => `Short ${index + 1} ${index ? `goes live in ${index * 5} hours` : 'starts now'}`)
                  .join('. ')}
                . Uses your source rights declaration and connected YouTube channel.
              </p>
            </div>
            <button
              className="button primary"
              disabled={scheduling || !isTerminal(job.status)}
              onClick={scheduleAll}
            >
              {scheduling ? 'Scheduling…' : `Schedule ${job.results.length} Shorts on YouTube ↗`}
            </button>
          </div>

          <div className="result-grid">
            {job.results.map(short => (
              <OutputCard key={short.id} short={short} onUpdated={updateShort} onRegenerate={() => action('regenerate')} />
            ))}
          </div>

          <p className="fine-print">
            Captions and metadata are generated automatically. Check the final videos and your rights to source
            footage, music, and third-party content before publishing.
          </p>
        </section>
      ) : (
        !busy && (
          <section className="how-it-works">
            <div className="section-heading">
              <h2>A considered edit, from start to finish.</h2>
              <span className="eyebrow">THE PROCESS</span>
            </div>
            <div className="process-grid">
              <div>
                <span className="step-number">01 / UNDERSTAND</span>
                <div className="process-art waveform" aria-hidden="true">
                  {WAVEFORM.map((n, i) => <i key={i} style={{ height: n + '%' }} />)}
                </div>
                <h3>Find the moments</h3>
                <p>Scene changes, speech and audio cues help surface distinct highlights.</p>
              </div>
              <div>
                <span className="step-number">02 / COMPOSE</span>
                <div className="process-art framing" aria-hidden="true">
                  <span />
                  <i />
                </div>
                <h3>Made for the vertical screen</h3>
                <p>Preserve context in 9:16, balance audio and add captions when available.</p>
              </div>
              <div>
                <span className="step-number">03 / REFINE</span>
                <div className="process-art timeline" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                  <span />
                </div>
                <h3>Review every final cut</h3>
                <p>Real previews, technical checks and clear rights notes. You make the final call.</p>
              </div>
            </div>
          </section>
        )
      )}

      <div className="rights-footer">
        <span aria-hidden="true">◈</span>
        <p>
          <strong>Creative control comes with responsibility.</strong> Permission, transformative editing and
          monetization eligibility are separate questions. Every output requires your final review.
        </p>
      </div>
    </>
  );
}
