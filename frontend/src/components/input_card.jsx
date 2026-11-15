import { useState } from 'react';
import EditingControls from './shorts/EditingControls';
import { EDITING_DEFAULTS, usesSecondVideo } from './shorts/options';

export default function InputCard({ onSubmit, busy, connected }) {
  const [tab, setTab] = useState('youtube');
  const [url, setUrl] = useState('');
  const [file, setFile] = useState(null);
  const [rights, setRights] = useState(false);
  const [reference, setReference] = useState('');
  const [options, setOptions] = useState({ caption_style: 'minimal', subtitle_mode: 'original', require_captions: true, target_duration: 30, resolution: '720', layout: 'preserve', mode: 'source_highlight', ...EDITING_DEFAULTS });
  const option = (key, value) => setOptions(old => ({ ...old, [key]: value }));
  function submit(event) {
    event.preventDefault();
    // A second video only applies to split-screen; never send a stale pick.
    const sent = usesSecondVideo(options) ? options : { ...options, secondary_asset_id: null };
    onSubmit({ url, file: tab === 'upload' ? file : null, rights_declaration: rights, permission_reference: reference, options: sent });
  }
  return <form className="input-card" onSubmit={submit}>
    <div className="source-tabs" role="tablist" aria-label="Source type"><button type="button" role="tab" aria-selected={tab === 'youtube'} onClick={() => setTab('youtube')}>YouTube URL</button><button type="button" role="tab" aria-selected={tab === 'upload'} onClick={() => setTab('upload')}>Upload footage</button><span>YOUR FOOTAGE. YOUR NEXT STORY.</span></div>
    <label className="input-label" htmlFor="source">{tab === 'youtube' ? 'Start with a video' : 'Choose your original footage'}</label>
    <div className="source-row">{tab === 'youtube' ? <div className="url-field"><span aria-hidden="true">↗</span><input id="source" type="url" required placeholder="https://www.youtube.com/watch?v=…" value={url} onChange={e => setUrl(e.target.value)} disabled={busy} /></div>
      : <input id="source" type="file" accept="video/*" required onChange={e => setFile(e.target.files[0])} disabled={busy} />}
      <button className="button primary" disabled={busy || !connected} type="submit">{busy ? 'Studio is working…' : 'Generate 3 Shorts'}<span aria-hidden="true">↗</span></button></div>
    <p className="input-help">Up to 30 minutes · Three distinct moments when the source allows · Audio transcribed locally</p>
    <label className="rights-checkbox"><input type="checkbox" required checked={rights} onChange={e => setRights(e.target.checked)} disabled={busy} /><span>I own this footage or have permission and the necessary commercial-use rights.</span></label>
    <details className="advanced"><summary><span>Editing preferences</span><span>Captions, duration & framing <b>+</b></span></summary>
      <div className="options-grid">
        <label>Editing mode<select value={options.mode} onChange={e => option('mode', e.target.value)}><option value="source_highlight">Source Highlight</option><option value="original_analysis">Original Analysis · configured provider required</option></select></label>
        <label className="wide rights-checkbox"><input type="checkbox" checked={options.require_captions} onChange={e => option('require_captions',e.target.checked)} />Require spoken captions; fail if no transcript can be generated</label>
        <label>Caption style<select value={options.caption_style} onChange={e => option('caption_style', e.target.value)}><option value="minimal">Premium Minimal</option><option value="gaming">Gaming Punch</option><option value="cinematic">Cinematic</option><option value="dynamic">Creator Dynamic</option></select></label>
        <label>Subtitle language<select value={options.subtitle_mode} onChange={e => option('subtitle_mode', e.target.value)}><option value="original">Original spoken language</option><option value="english">English translation</option><option value="hindi">Hindi translation</option><option value="bilingual">Hindi + English</option></select></label>
        <label>Target duration<select value={options.target_duration} onChange={e => option('target_duration', Number(e.target.value))}><option value={20}>20 seconds</option><option value={30}>30 seconds</option><option value={45}>45 seconds</option><option value={60}>60 seconds</option></select></label>
        <label>Output resolution<select value={options.resolution} onChange={e => option('resolution', e.target.value)}><option value="720">720 × 1280 · efficient</option><option value="1080">1080 × 1920 · source permitting</option></select></label>
        <label>Vertical framing<select value={options.layout} onChange={e => option('layout', e.target.value)}><option value="preserve">Full frame · blurred background</option><option value="center">Center crop · review action placement</option></select></label>
        <label className="wide">Permission or license reference<input value={reference} maxLength={2000} onChange={e => setReference(e.target.value)} placeholder="License reference, permission notes or ownership details" /></label>
      </div>
      <EditingControls options={options} onChange={option} disabled={busy} />
      <p className="option-note">Source Highlight mode preserves original audio. Captions are generated from its audio on the worker. Translation and original commentary need a configured provider; voice synthesis is not available in this release.</p>
    </details>
  </form>;
}
