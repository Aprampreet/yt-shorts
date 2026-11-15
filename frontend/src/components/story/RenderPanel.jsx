import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { asset } from '../api';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Field from '../ui/Field';
import TimelineSummary from '../shorts/TimelineSummary';
import ErrorNote from './ErrorNote';
import RightsReport from './RightsReport';
import useAction from './useAction';
import { CAPTION_PRESETS, THEMES, scriptTitle } from './constants';
import { approvedScripts } from './steps';
import { isTerminal } from '../../hooks/useJobPolling';
import { getJob, renderProject, updateProject } from '../../api/story';
import { listAssets } from '../../api/assets';

const QA_TONE = { passed: 'success', flagged: 'warn', failed: 'danger' };

function VideoCard({ video, scriptName }) {
  return (
    <article className="result-card story-video">
      <div className="video-wrap">
        <video controls preload="metadata" poster={asset(video.thumbnail_url)} src={asset(video.video_url)} aria-label={`Preview: ${video.title}`} />
        <span className="clip-length">{Number(video.duration).toFixed(1)}s</span>
      </div>
      <div className="result-body">
        <div className="eyebrow">STORY VIDEO {video.short_number}{scriptName ? ` · ${scriptName}` : ''}</div>
        <h3>{video.title}</h3>
        <div className="badge-row">
          <Badge tone={QA_TONE[video.qa_status] || 'neutral'}>Technical QA: {video.qa_status}</Badge>
          <span className="muted">Script heuristic {Number(video.quality_score || 0).toFixed(2)} · not a view prediction</span>
        </div>
        <TimelineSummary summary={video.timeline_summary} qaStatus={video.qa_status} />
        <a className="button download" href={asset(video.download_url)}>Download MP4</a>
        <RightsReport video={video} />
      </div>
    </article>
  );
}

/** Step 7: look, render, results. Progress and per-video failures come from the job, never from the client. */
export default function RenderPanel({ project, busyJob, onStart, onChanged }) {
  const inputs = project.inputs || {};
  const ready = useMemo(() => approvedScripts(project).filter(s => s.narration && s.storyboard_ready), [project]);
  const [theme, setTheme] = useState(inputs.theme || 'gaming_hype');
  const [caption, setCaption] = useState(inputs.caption_preset || 'clean_commentary');
  const [music, setMusic] = useState(inputs.music_asset_id || '');
  const [tracks, setTracks] = useState([]);
  const [chosen, setChosen] = useState(() => ready.slice(0, 3).map(s => s.id));
  const [failures, setFailures] = useState([]);
  const look = useAction();
  const render = useAction();

  useEffect(() => {
    let current = true;
    listAssets({ limit: 100 })
      .then(data => { if (current) setTracks((data?.items || []).filter(a => (a.kind === 'music' || a.kind === 'audio') && a.license_status !== 'restricted')); })
      .catch(() => { if (current) setTracks([]); });
    return () => { current = false; };
  }, []);

  const lastRender = (project.jobs || []).find(j => j.step === 'render' && isTerminal(j.status));
  useEffect(() => {
    let current = true;
    setFailures([]);
    if (lastRender) {
      getJob(lastRender.id)
        .then(job => { if (current) setFailures(job?.analysis?.failures || []); })
        .catch(() => {});
    }
    return () => { current = false; };
  }, [lastRender?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = id => setChosen(old => (old.includes(id) ? old.filter(x => x !== id) : old.length >= 3 ? old : [...old, id]));
  const names = Object.fromEntries((project.scripts || []).map((s, i) => [s.id, scriptTitle(s, i)]));
  const videos = project.videos || [];

  async function saveLook() {
    const done = await look.run(() => updateProject(project.id, { theme, caption_preset: caption, music_asset_id: music }));
    if (done) onChanged();
  }

  return (
    <section className="panel" aria-labelledby="render-title">
      <h2 className="panel-title" id="render-title">Render</h2>

      <div className="form-grid">
        <Field label="Theme">
          <select value={theme} onChange={e => setTheme(e.target.value)}>
            {THEMES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </Field>
        <Field label="Caption preset">
          <select value={caption} onChange={e => setCaption(e.target.value)}>
            {CAPTION_PRESETS.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </Field>
        <Field label="Background music (optional)" className="wide">
          <select value={music} onChange={e => setMusic(e.target.value)}>
            <option value="">No music</option>
            {tracks.map(a => <option key={a.id} value={a.id}>{a.title} · {a.license_label}</option>)}
          </select>
        </Field>
      </div>
      <ErrorNote error={look.error} />
      <div className="form-actions">
        <Button busy={look.busy} disabled={busyJob} onClick={saveLook}>Save look</Button>
      </div>

      <fieldset className="asset-pick">
        <legend>Scripts to render (up to 3)</legend>
        {ready.map(script => (
          <label className="check" key={script.id}>
            <input type="checkbox" checked={chosen.includes(script.id)} onChange={() => toggle(script.id)} />
            <span>{names[script.id]}</span>
          </label>
        ))}
      </fieldset>
      <ErrorNote error={render.error} />
      <div className="form-actions">
        <Button variant="primary" busy={render.busy} disabled={busyJob || !chosen.length}
          onClick={() => render.run(() => onStart(() => renderProject(project.id, chosen)))}>
          Render videos
        </Button>
        <span className="muted">Rendering takes a while. Progress is shown above while it runs.</span>
      </div>

      {failures.length > 0 && (
        <div className="story-error" role="alert">
          <strong>{failures.length === 1 ? 'One video could not be rendered' : `${failures.length} videos could not be rendered`}</strong>
          <ul>
            {failures.map(f => (
              <li key={`${f.script_id}-${f.number}`}>
                <span className="muted">{names[f.script_id] || `Video ${f.number}`} ({f.error})</span>
                <p>{f.message}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
      {lastRender?.status === 'failed' && failures.length === 0 && lastRender.error_message && (
        <p className="story-error" role="alert">{lastRender.error_message}</p>
      )}

      {videos.length > 0 && (
        <>
          <h3 className="sub-title">Rendered videos</h3>
          <div className="result-grid">
            {videos.map(video => <VideoCard key={video.id} video={video} scriptName={names[video.script_id]} />)}
          </div>
          <p className="muted">Review each video and its rights block before you publish.</p>
          <Link className="button primary" to="/publish">Schedule in Publishing Studio</Link>
        </>
      )}
    </section>
  );
}
