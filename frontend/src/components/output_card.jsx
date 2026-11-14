import { useState, useEffect } from 'react';
import { api, asset } from './api';

export default function OutputCard({ short, onUpdated, onRegenerate, onSchedule }) {
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [segments, setSegments] = useState(short.segments || []);
  useEffect(() => { setSegments(short.segments || []); }, [short.segments]);
  const editSegment = (id, field, value) => setSegments(old => old.map(s => s.id === id ? { ...s, [field]: value } : s));
  async function saveTranscript() {
    setBusy(true); setMessage('');
    try {
      const updated = await api(`/shorts/${short.id}/transcript`, { method: 'PUT', body: JSON.stringify({ segments }) });
      onUpdated(updated); setMessage('Edited captions are rendering. Check the result again shortly.');
      for (let attempt = 0; attempt < 60; attempt++) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        const next = await api(`/shorts/${short.id}`);
        if (next.quality_report.transcript_edit_status !== 'rendering') { onUpdated(next); setMessage(next.quality_report.transcript_edit_status === 'ready' ? 'Edited captions are ready in the video and downloads.' : (next.quality_report.transcript_edit_error || 'Caption render failed.')); break; }
      }
    } catch (e) { setMessage(e.message); }
    finally { setBusy(false); }
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); setMessage('Copied to clipboard'); }
    catch { setMessage('Clipboard unavailable. Copy from the details below.'); }
  }
  async function saveDraft() {
    setBusy(true); setMessage('');
    try { await api('/social/posts',{method:'POST',body:JSON.stringify({short_id:short.id,platform:'youtube',title:short.title,description:short.description,hashtags:short.hashtags,timezone:'Asia/Kolkata'})});setMessage('Saved as a YouTube draft. Review rights, then approve it in Schedule.');onSchedule(); }
    catch(e){setMessage(e.message);} finally{setBusy(false);}
  }
  async function reviewRights() {
    if (!window.confirm('I reviewed the final video and confirm source permission and music rights. Continue?')) return;
    try {onUpdated(await api(`/shorts/${short.id}/rights-review?permission_verified=true&music_verified=true`,{method:'POST'}));setMessage('Rights review recorded. This is your declaration, not independent verification.');}
    catch(e){setMessage(e.message);}
  }
  async function metadata() {
    setBusy(true); setMessage('');
    try { onUpdated(await api(`/shorts/${short.id}/metadata`, { method: 'POST' })); }
    catch (e) { setMessage(e.message); }
    finally { setBusy(false); }
  }
  const start = `${Math.floor(short.start_time / 60)}:${String(Math.floor(short.start_time % 60)).padStart(2, '0')}`;
  return <article className="result-card">
    <div className="video-wrap"><video controls preload="metadata" poster={asset(short.thumbnail_url)} src={asset(short.video_url)} aria-label={`Preview Short ${short.short_number}`} />
      <span className="clip-number">0{short.short_number}</span><span className="clip-length">{short.duration.toFixed(1)}s</span></div>
    <div className="result-body"><div className="eyebrow">SOURCE HIGHLIGHT · {start}</div><h3>{short.title}</h3>
      <div className="scores"><span>Highlight <b>{short.quality_report.highlight_score ?? '—'}</b></span><span>Technical QA <b>{short.quality_score}/100</b></span></div>
      <div className="review-label">◌ Manual rights review required</div>
      <a className="button download" href={asset(short.download_url)}>↓ Download MP4</a>
      <div className="result-actions"><button onClick={() => copy(short.title)}>Copy title</button><button onClick={() => copy(short.description)}>Copy description</button>
        {short.caption_url && <a href={asset(short.caption_url)}>SRT captions</a>}
        {short.caption_url && <a href={asset(short.caption_url + '?format=ass')}>ASS captions</a>}
        <button disabled={busy} onClick={metadata}>{busy ? 'Generating…' : 'Regenerate metadata'}</button><button onClick={onRegenerate}>Regenerate job</button><button onClick={saveDraft}>Save YouTube draft</button><button onClick={reviewRights}>Record rights review</button></div>
      {message && <p role="status" className="action-message">{message}</p>}
      {segments.length > 0 && <details className="transcript-editor"><summary>Review & edit transcript</summary>
        <p>Correct spoken text and translations. Saving re-renders the MP4 from the retained source.</p>
        {segments.map(segment => <div className="segment-edit" key={segment.id}><small>{(segment.start_ms/1000).toFixed(1)}–{(segment.end_ms/1000).toFixed(1)}s · {segment.detected_language}</small>
          <label>Spoken text<textarea value={segment.original_text} onChange={e => editSegment(segment.id, 'original_text', e.target.value)} /></label>
          <label>English<textarea value={segment.translated_english} onChange={e => editSegment(segment.id, 'translated_english', e.target.value)} /></label>
          <label>Hindi<textarea value={segment.translated_hindi} onChange={e => editSegment(segment.id, 'translated_hindi', e.target.value)} /></label></div>)}
        <button disabled={busy} onClick={saveTranscript}>{busy ? 'Rendering…' : 'Save & re-render captions'}</button>
      </details>}
      <details><summary>Editing & quality report</summary><p>{short.description}</p><p>{short.hashtags.join(' ')}</p>
        <p>{short.quality_report.decision}</p><ul>{short.quality_report.transformations?.map(t => <li key={t}>{t}</li>)}</ul>
        <p>{short.quality_report.caption_timing}</p>
        {short.quality_report.flags?.map(f => <p key={f}>{f}</p>)}
        {short.quality_report.rights_review && <p>Music rights: {short.quality_report.rights_review.music_rights}. Permission: {short.quality_report.rights_review.permission_verification}. Third-party footage: {short.quality_report.rights_review.third_party_footage}.</p>}
        <p>Requires human review: {short.quality_report.unverified?.join('; ')}.</p></details>
    </div>
  </article>;
}
