import { useCallback, useEffect, useState } from 'react';
import InputCard from './components/input_card';
import OutputCard from './components/output_card';
import ProgressBar from './components/progressbar';
import Scheduler from './components/scheduler';
import { api, initialize, asset } from './components/api';

const terminal = ['failed', 'cancelled', 'completed', 'needs_review'];
export default function App() {
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [job, setJob] = useState(null);
  const [history, setHistory] = useState([]);
  const [view, setView] = useState(new URLSearchParams(window.location.search).get('view') === 'schedule' ? 'schedule' : 'studio');
  const [submitting, setSubmitting] = useState(false);
  const busy = submitting || !!(job && !terminal.includes(job.status));
  const loadHistory = useCallback(async () => { setHistory(await api('/jobs')); }, []);
  useEffect(() => {
    let mounted = true;
    initialize().then(async () => {
      const jobs = await api('/jobs');
      if (mounted) { setConnected(true); setHistory(jobs); setJob(jobs.find(j => !terminal.includes(j.status)) || null); }
    }).catch(e => { if (mounted) setError(e.message); });
    return () => { mounted = false; };
  }, []);
  const activeJobId = job?.id;
  const activeJobStatus = job?.status;
  useEffect(() => {
    if (!activeJobId || terminal.includes(activeJobStatus)) return;
    let stopped = false;
    let timer;
    async function poll() {
      try {
        const next = await api(`/jobs/${activeJobId}`);
        if (!stopped) {
          setJob(next);
          if (terminal.includes(next.status)) { await loadHistory(); return; }
        }
      } catch (e) { if (!stopped) setError(e.message); }
      if (!stopped) timer = setTimeout(poll, 2000);
    }
    timer = setTimeout(poll, 1000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [activeJobId, activeJobStatus, loadHistory]); // Poll only the active job, never simulate progress.
  async function create(data) {
    setError(''); setSubmitting(true);
    try {
      let body;
      let path = '/jobs';
      if (data.file) {
        body = new FormData(); body.append('file', data.file); body.append('rights_declaration', String(data.rights_declaration));
        body.append('permission_reference', data.permission_reference); body.append('options', JSON.stringify(data.options)); path = '/uploads';
      } else { body = JSON.stringify(data); }
      setJob(await api(path, { method: 'POST', body })); await loadHistory();
    } catch (e) { setError(e.message); }
    finally { setSubmitting(false); }
  }
  async function action(name) {
    setSubmitting(true); setError('');
    try { setJob(await api(`/jobs/${job.id}/${name}`, { method: 'POST' })); await loadHistory(); }
    catch (e) { setError(e.message); }
    finally { setSubmitting(false); }
  }
  function updateShort(short) { setJob(old => ({ ...old, results: old.results.map(s => s.id === short.id ? short : s) })); }
  return <div className="app-shell">
    <aside className="sidebar"><a href="#" className="brand" aria-label="Framecut Studio home"><span className="brand-mark">f<span>c</span></span><span>framecut<span className="brand-sub">STUDIO</span></span></a>
      <div className="workspace-label">WORKSPACE</div><nav aria-label="Main navigation"><button className={view === 'studio' ? 'selected' : ''} onClick={() => setView('studio')}><span>▣</span>Create Shorts<span className="nav-arrow">↗</span></button><button className={view === 'history' ? 'selected' : ''} onClick={() => { setView('history'); loadHistory().catch(e => setError(e.message)); }}><span>◷</span>Recent projects<span className="history-count">{history.length}</span></button><button className={view === 'schedule' ? 'selected' : ''} onClick={() => setView('schedule')}><span>▦</span>Schedule</button></nav>
      <div className="sidebar-bottom"><div className="local-badge"><i className={connected ? 'online' : ''} />{connected ? 'Studio connected' : 'API disconnected'}</div><p>Hosted intelligence.<br />Your footage stays in your studio.</p><div className="profile"><span>Y</span><div>Your workspace<small>Local creator studio</small></div></div></div>
    </aside>
    <main><header className="topbar"><span>Workspace <b>/</b> {view === 'studio' ? 'Create Shorts' : view === 'schedule' ? 'Schedule' : 'Recent projects'}</span><span className="version">STUDIO / 01</span></header>
      <div className="main-content"><div className="page-heading"><div className="eyebrow"><span className="accent-line" /> LONG FORM. NEW POSSIBILITIES.</div><h1>{view === 'studio' ? <>Good footage.<br /><span>Three fresh perspectives.</span></> : view === 'schedule' ? <>Your calendar.<br /><span>Approved posts only.</span></> : <>Your editing room.<br /><span>Pick up where you left off.</span></>}</h1><p>{view === 'studio' ? 'Turn your authorized footage into vertical, ready-to-review Shorts.' : view === 'schedule' ? 'Connect a destination, review rights and approve each upload.' : 'Reopen your generations, review the edits and download retained footage.'}</p></div>
      {error && <div className="error-banner" role="alert">{error}<button aria-label="Dismiss error" onClick={() => setError('')}>×</button></div>}
      {view === 'schedule' ? <Scheduler /> : view === 'studio' ? <>
        <InputCard onSubmit={create} busy={busy} connected={connected} />
        {job && !terminal.includes(job.status) && <ProgressBar job={job} onCancel={() => action('cancel')} />}
        {job?.status === 'failed' && <section className="failure" role="alert"><h2>{job.stage}</h2><p>{job.error_message}</p><button disabled={busy} onClick={() => action('retry')}>Retry generation ↗</button></section>}
        {job?.status === 'cancelled' && <p className="notice">Generation cancelled. The worker stops at the next processing boundary.</p>}
        {job?.analysis?.warnings?.length > 0 && <details className="warning-panel" open={terminal.includes(job.status)}><summary>Generation notes · {job.analysis.warnings.length}</summary>{job.analysis.warnings.map(w => <p key={w}>{w}</p>)}</details>}
        {job?.results?.length > 0 ? <section className="results"><div className="section-heading"><div><span className="eyebrow">THE FINAL CUTS</span><h2>{job.results.length} moments. Ready for your review.</h2></div><a className="button secondary" href={asset(`/api/v1/jobs/${job.id}/download`)}>↓ Download all</a></div><div className="result-grid">{job.results.map(short => <OutputCard key={short.id} short={short} onUpdated={updateShort} onRegenerate={() => action('regenerate')} onSchedule={() => setView('schedule')} />)}</div><p className="fine-print">Highlight and quality scores are heuristics. Review captions, framing, source rights and third-party content before publishing.</p></section>
          : !busy && <section className="how-it-works"><div className="section-heading"><h2>A considered edit, from start to finish.</h2><span className="eyebrow">THE PROCESS</span></div><div className="process-grid"><div><span className="step-number">01 / UNDERSTAND</span><div className="process-art waveform" aria-hidden="true">{[18,30,20,45,65,35,80,50,28,63,90,40,60,32,18,48,70,37,24,15].map((n,i) => <i key={i} style={{height:n+'%'}} />)}</div><h3>Find the moments</h3><p>Scene changes, speech and audio cues help surface distinct highlights.</p></div><div><span className="step-number">02 / COMPOSE</span><div className="process-art framing" aria-hidden="true"><span /><i /></div><h3>Made for the vertical screen</h3><p>Preserve context in 9:16, balance audio and add captions when available.</p></div><div><span className="step-number">03 / REFINE</span><div className="process-art timeline" aria-hidden="true"><i/><i/><i/><span/></div><h3>Review every final cut</h3><p>Real previews, technical checks and clear rights notes. You make the final call.</p></div></div></section>}
        <div className="rights-footer"><span>◈</span><p><strong>Creative control comes with responsibility.</strong> Permission, transformative editing and monetization eligibility are separate questions. Every output requires your final review.</p></div>
      </> : <section className="history-list"><div className="section-heading"><h2>Recent generations</h2><button onClick={() => loadHistory().catch(e => setError(e.message))}>Refresh ↻</button></div>{history.length ? history.map(item => <button className="history-row" key={item.id} onClick={() => { setJob(item); setView('studio'); }}><span className="history-icon">▤</span><span><strong>{item.title}</strong><small>{new Date(item.created_at).toLocaleString()} · {item.results.length} Shorts</small></span><span className={`status-pill ${item.status}`}>{item.status.replaceAll('_',' ')}</span><span>↗</span></button>) : <p className="empty-history">Your first project starts with a source. Create a generation to see it here.</p>}</section>}
      <footer>FRAMECUT STUDIO<span>Thoughtful edits. Your final say.</span></footer></div>
    </main>
  </div>;
}
