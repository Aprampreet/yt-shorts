import { useEffect, useState } from 'react';
import { asset } from '../api';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import LicenseBadge from '../library/LicenseBadge';
import ErrorNote from './ErrorNote';
import SceneEditor from './SceneEditor';
import useAction from './useAction';
import { TRANSITIONS, VISUAL_TYPES, formatSeconds } from './constants';
import { buildStoryboard } from '../../api/story';
import { listAssets } from '../../api/assets';

const label = (list, value) => list.find(item => item.value === value)?.label || value;
const VISUAL_KINDS = ['video', 'image', 'graphic'];

function SceneRow({ scene, assets, editing, onEdit, onSaved, onCancel }) {
  const a = scene.asset;
  return (
    <li className="scene">
      <div className="scene-thumb">
        {a?.thumbnail_url
          ? <img src={asset(a.thumbnail_url)} alt={`Thumbnail of ${a.title}`} loading="lazy" />
          : <span className="muted">{a ? a.kind : 'Graphic'}</span>}
      </div>
      <div className="scene-body">
        <div className="badge-row">
          <strong>Scene {scene.order}</strong>
          <span className="muted">{formatSeconds(scene.start)} to {formatSeconds(scene.end)}</span>
          <Badge tone={scene.visual_type === 'source_clip' ? 'warn' : 'accent'}>{label(VISUAL_TYPES, scene.visual_type)}</Badge>
          {a ? <LicenseBadge asset={a} /> : <Badge>Original graphic</Badge>}
          <span className="muted">Transition: {label(TRANSITIONS, scene.transition)}</span>
        </div>
        <p>{scene.narration_text}</p>
        {a && <p className="muted">Asset: {a.title}</p>}
        {scene.attribution && <p className="muted">Attribution: {scene.attribution}</p>}
        {a?.third_party_source_footage && <p className="muted">Third-party source footage.</p>}
        {editing
          ? <SceneEditor scene={scene} assets={assets} onSaved={onSaved} onCancel={onCancel} />
          : <Button size="sm" onClick={onEdit} aria-label={`Edit scene ${scene.order}`}>Edit scene</Button>}
      </div>
    </li>
  );
}

/** Step 6: build the storyboard, then review and edit each scene. Errors from the backend show inline. */
export default function StoryboardEditor({ project, script, busyJob, onChanged }) {
  const [library, setLibrary] = useState([]);
  const [preferred, setPreferred] = useState([]);
  const [useClips, setUseClips] = useState(false);
  const [scenes, setScenes] = useState(script.scenes || []);
  const [report, setReport] = useState(null);
  const [editing, setEditing] = useState(null);
  const build = useAction();

  useEffect(() => { setScenes(script.scenes || []); }, [script.scenes]);
  useEffect(() => {
    let current = true;
    listAssets({ limit: 100 })
      .then(data => { if (current) setLibrary(data?.items || []); })
      .catch(() => { if (current) setLibrary([]); });
    return () => { current = false; };
  }, []);

  const canUseClips = Boolean(project.source?.url && project.source?.rights_declaration);
  const visuals = library.filter(a => VISUAL_KINDS.includes(a.kind) && a.origin !== 'source_ingest' && a.license_status !== 'restricted');
  const duration = script.narration?.duration || 0;
  const clipSeconds = scenes.filter(s => s.visual_type === 'source_clip').reduce((sum, s) => sum + (s.end - s.start), 0);
  const cap = project.limits?.source_clip_max_share ?? 0.35;
  const share = duration ? clipSeconds / duration : 0;

  async function doBuild() {
    const result = await build.run(() => buildStoryboard(script.id, { asset_ids: preferred, use_source_clips: canUseClips && useClips }));
    if (result) {
      setScenes(result.scenes || []);
      setReport(result.report || null);
      setEditing(null);
      onChanged();
    }
  }

  function saved(updated) {
    setScenes(old => old.map(s => (s.id === updated.id ? updated : s)));
    setEditing(null);
    onChanged();
  }

  const toggle = id => setPreferred(old => (old.includes(id) ? old.filter(x => x !== id) : [...old, id]));

  return (
    <section className="panel" aria-labelledby="board-title">
      <h2 className="panel-title" id="board-title">Storyboard</h2>
      <p className="muted">
        Scenes follow your narration. The studio uses original graphics first and your library media where you allow it.
      </p>

      {visuals.length > 0 && (
        <fieldset className="asset-pick">
          <legend>Preferred library assets (optional)</legend>
          {visuals.map(a => (
            <label className="check" key={a.id}>
              <input type="checkbox" checked={preferred.includes(a.id)} onChange={() => toggle(a.id)} />
              <span>{a.title} <LicenseBadge asset={a} /></span>
            </label>
          ))}
        </fieldset>
      )}

      {canUseClips ? (
        <label className="check">
          <input type="checkbox" checked={useClips} onChange={e => setUseClips(e.target.checked)} />
          <span>
            Allow short clips from the source video. It is third-party footage covered by your rights declaration, tied to
            claims, and limited to {Math.round(cap * 100)}% of the video.
          </span>
        </label>
      ) : (
        <p className="muted">Source clips are off: this project has no source video with a rights declaration.</p>
      )}

      <ErrorNote error={build.error} />
      <div className="form-actions">
        <Button variant="primary" busy={build.busy} disabled={busyJob} onClick={doBuild}>
          {scenes.length ? 'Rebuild storyboard' : 'Build storyboard'}
        </Button>
        {scenes.length > 0 && <span className="muted">Rebuilding replaces your scene edits.</span>}
      </div>

      {report && (
        <div className="story-callout" role="status">
          <strong>{report.scenes} scenes planned</strong>
          {(report.notes || []).map(note => <p key={note} className="muted">{note}</p>)}
        </div>
      )}

      {scenes.length > 0 && (
        <>
          <p className={share > cap ? 'story-error' : 'muted'} aria-live="polite">
            Third-party source clips: {Math.round(share * 100)}% of the video (limit {Math.round(cap * 100)}%).
          </p>
          <ol className="scene-list" aria-label="Scenes">
            {scenes.map(scene => (
              <SceneRow
                key={scene.id}
                scene={scene}
                assets={library}
                editing={editing === scene.id}
                onEdit={() => setEditing(scene.id)}
                onSaved={saved}
                onCancel={() => setEditing(null)}
              />
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
