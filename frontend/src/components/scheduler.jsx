import { useEffect, useState } from 'react';
import { api } from './api';

export default function Scheduler({ initialFilter = 'calendar' }) {
  const [posts,setPosts] = useState([]);
  const [accounts,setAccounts] = useState([]);
  const [preferences,setPreferences] = useState({timezone:'Asia/Kolkata',slots:['12:00','17:00','21:00']});
  const [suggestions,setSuggestions] = useState([]);
  const [filter,setFilter] = useState(initialFilter);
  const [month,setMonth] = useState(new Date().getMonth());
  const [year,setYear] = useState(new Date().getFullYear());
  function moveMonth(change) { const next = new Date(year,month+change,1);setMonth(next.getMonth());setYear(next.getFullYear()); }
  const [error,setError] = useState('');
  const [saving,setSaving] = useState(false);
  const [drafts,setDrafts] = useState({});
  const edit = (post,field,value) => setDrafts(old => ({...old,[post.id]:{...post,...old[post.id],[field]:value}}));
  async function refresh() {
    const [nextPosts,nextAccounts,nextPreferences,nextSuggestions] = await Promise.all([
      api('/social/posts'),api('/social/accounts'),api('/social/preferences'),api('/social/suggestions')]);
    setPosts(nextPosts);setAccounts(nextAccounts);setPreferences(nextPreferences);setSuggestions(nextSuggestions);
  }
  useEffect(() => {
    refresh().catch(e => setError(e.message));
    const timer = window.setInterval(() => api('/social/posts').then(setPosts).catch(e => setError(e.message)), 10000);
    return () => window.clearInterval(timer);
  }, []);
  async function approveAll() {
    const ids = posts.filter(p => p.status === 'draft' && p.account_id).map(p => p.id);
    if (!ids.length) { setError('Assign a connected account and complete rights review before approving drafts.'); return; }
    try {await api('/social/approve-batch',{method:'POST',body:JSON.stringify({post_ids:ids})});await refresh();setError('Approved drafts queued for YouTube upload.');}
    catch(e){setError(e.message);}
  }
  async function connect() {
    try { const result = await api('/social/youtube/connect'); window.location.href = result.authorization_url; }
    catch(e) {setError(e.message);}
  }
  async function savePreferences() {
    setSaving(true);
    try { await api('/social/preferences',{method:'PUT',body:JSON.stringify(preferences)});await refresh();setError('Posting slots saved.'); }
    catch(e){setError(e.message);} finally{setSaving(false);}
  }
  async function savePost(post) {
    const draft = {...post,...drafts[post.id]};
    try { await api(`/social/posts/${post.id}`,{method:'PUT',body:JSON.stringify({title:draft.title,description:draft.description,hashtags:draft.hashtags,
      account_id:draft.account_id || null,scheduled_at:draft.scheduled_at || null,timezone:preferences.timezone})});await refresh();setError('Draft saved.'); }
    catch(e){setError(e.message);}
  }
  async function act(post,command) {
    try { await api(`/social/posts/${post.id}/${command}`,{method:'POST'});await refresh(); }
    catch(e){setError(e.message);}
  }
  async function disconnect(account) {
    try {await api(`/social/accounts/${account.id}/disconnect`,{method:'POST'});await refresh();}
    catch(e){setError(e.message);}
  }
  const shown = posts.filter(post => filter === 'calendar' || filter === 'all' ||
    (filter === 'queue' && ['queued','scheduled','uploading','uploaded_private'].includes(post.status)) ||
    (filter === 'drafts' && post.status === 'draft') ||
    (filter === 'published' && ['published'].includes(post.status)) ||
    (filter === 'failed' && post.status === 'failed'));
  return <section className="schedule-page"><div className="section-heading"><div><span className="eyebrow">PUBLISHING ROOM</span><h2>YouTube upload queue</h2></div></div>
    {error && <p className="warning-panel" role="status">{error}</p>}
    <div className="scheduler-grid"><div className="scheduler-panel"><h3>Connected accounts</h3>{accounts.length ? accounts.map(account => <div className="social-row" key={account.id}><span>{account.platform} · {account.account_name}</span><button onClick={() => disconnect(account)}>Disconnect</button></div>) : <p>No accounts connected.</p>}
      {!accounts.some(account => account.platform === 'youtube') && <button className="button secondary" onClick={connect}>Connect YouTube ↗</button>}<p className="option-note">Connect your YouTube channel once. Access is refreshed automatically while Google's authorization remains valid.</p>
      <div className="social-row"><span>Instagram</span><small>Requires eligible professional account, app approval and public media hosting.</small></div>
    </div><div className="scheduler-panel"><h3>Posting slots</h3><label>Timezone<input value={preferences.timezone} onChange={e => setPreferences({...preferences,timezone:e.target.value})} /></label>
      <label>Daily times, HH:MM<input value={preferences.slots.join(', ')} onChange={e => setPreferences({...preferences,slots:e.target.value.split(',').map(s => s.trim())})} /></label>
      <button className="button secondary" disabled={saving} onClick={savePreferences}>Save slots</button><p>Next open slots: {suggestions.map(s => new Date(s.local).toLocaleString()).join(' · ') || '—'}</p></div></div>
    <button className="button secondary" onClick={approveAll}>Approve all ready drafts</button><div className="schedule-tabs">{[['calendar','Calendar'],['queue','Scheduled queue'],['drafts','Drafts'],['published','Published'],['failed','Failed']].map(([key,label]) => <button className={filter === key ? 'selected' : ''} key={key} onClick={() => setFilter(key)}>{label}</button>)}</div>
    {filter === 'calendar' && <div className="calendar-view"><div className="calendar-head"><button onClick={() => moveMonth(-1)}>←</button><strong>{new Date(year,month,1).toLocaleString('en-IN',{month:'long',year:'numeric'})}</strong><button onClick={() => moveMonth(1)}>→</button></div><div className="calendar-grid">{['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(day => <span className="calendar-day" key={day}>{day}</span>)}{Array.from({length:new Date(year,month,1).getDay()},(_,index) => <span key={'blank-'+index} />)}{Array.from({length:new Date(year,month+1,0).getDate()},(_,index) => {const date=index+1;const dayPosts=posts.filter(post => {if(!post.scheduled_at)return false;const local=new Date(post.scheduled_at);return local.getFullYear()===year && local.getMonth()===month && local.getDate()===date;});return <div className="calendar-cell" key={date}><strong>{date}</strong>{dayPosts.map(post => <span key={post.id}>{post.platform} · {post.title.slice(0,22)} · {post.status}</span>)}</div>;})}</div></div>}
    <div className="schedule-items">{shown.length ? shown.map(post => <div className="schedule-item" key={post.id}><div><span className="eyebrow">{post.platform.toUpperCase()} · {post.status.toUpperCase()}</span><h3>{post.title}</h3><p>{post.scheduled_at ? new Date(post.scheduled_at).toLocaleString('en-IN',{timeZone:post.timezone}) + ' · '+post.timezone : 'Upload immediately'}</p><p>{post.last_error}</p></div><div>{post.status === 'draft' && <div className="post-editor"><label>Title<input value={drafts[post.id]?.title ?? post.title} onChange={e => edit(post,'title',e.target.value)} /></label><label>Description<textarea value={drafts[post.id]?.description ?? post.description} onChange={e => edit(post,'description',e.target.value)} /></label><label>Account<select value={drafts[post.id]?.account_id ?? post.account_id ?? ''} onChange={e => edit(post,'account_id',e.target.value)}><option value="">Choose a connected account</option>{accounts.filter(a => a.platform === post.platform).map(a => <option key={a.id} value={a.id}>{a.account_name}</option>)}</select></label><label>Publishing time<select value={drafts[post.id]?.scheduled_at ?? post.scheduled_at ?? ''} onChange={e => edit(post,'scheduled_at',e.target.value)}><option value="">Upload immediately after approval</option>{suggestions.map(slot => <option key={slot.utc} value={slot.utc}>{new Date(slot.local).toLocaleString('en-IN',{timeZone:preferences.timezone})}</option>)}</select></label><button onClick={() => savePost(post)}>Save changes</button></div>}{post.status === 'draft' && <button onClick={() => act(post,'approve')}>Approve upload</button>}{post.status === 'failed' && <button onClick={() => act(post,'approve')}>Retry</button>}{['draft','queued','failed'].includes(post.status) && <button onClick={() => act(post,'cancel')}>Cancel</button>}{post.published_url && <a href={post.published_url} target="_blank" rel="noreferrer">View on YouTube ↗</a>}</div></div>) : <p>No {filter === 'calendar' ? '' : filter} posts yet. Generate Shorts, then use Schedule on YouTube.</p>}</div>
    <p className="fine-print">YouTube schedules approved uploads natively. Instagram scheduling needs an always-on worker and publicly accessible approved media; this local studio does not publish Reels yet.</p></section>;
}
