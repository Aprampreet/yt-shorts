import { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Link, Navigate, Route, Routes, useSearchParams } from 'react-router-dom';
import { api, initialize } from './components/api';
import useJobPolling, { isTerminal } from './hooks/useJobPolling';
import Sidebar from './layout/Sidebar';
import Topbar from './layout/Topbar';
import PageHeading from './layout/PageHeading';
import Notices from './layout/Notices';
import ShortsPage from './pages/ShortsPage';
import HistoryPage from './pages/HistoryPage';
import StoryStudioPage from './pages/StoryStudioPage';
import PublishingPage from './pages/PublishingPage';
import MediaLibraryPage from './pages/MediaLibraryPage';

/**
 * The backend OAuth callback still redirects to `/?view=schedule&connected=…`.
 * Send that to `/publish`, keeping every other query parameter.
 */
function HomeRoute({ children }) {
  const [params] = useSearchParams();
  if (params.get('view') === 'schedule') {
    const rest = new URLSearchParams(params);
    rest.delete('view');
    const query = rest.toString();
    return <Navigate replace to={`/publish${query ? `?${query}` : ''}`} />;
  }
  return children;
}

function NotFoundPage() {
  return (
    <>
      <PageHeading eyebrow="NOT FOUND" title="This page does not exist." />
      <Link className="button secondary" to="/">Go to Create Shorts</Link>
    </>
  );
}

function Shell() {
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [job, setJob] = useState(null);
  const [history, setHistory] = useState([]);
  const [scheduleMessage, setScheduleMessage] = useState('');

  const loadHistory = useCallback(async () => {
    setHistory(await api('/jobs'));
  }, []);
  const refreshHistory = useCallback(() => {
    loadHistory().catch(e => setError(e.message));
  }, [loadHistory]);

  useEffect(() => {
    let mounted = true;
    initialize()
      .then(async () => {
        const jobs = await api('/jobs');
        if (mounted) {
          setConnected(true);
          setHistory(jobs);
          setJob(jobs.find(j => !isTerminal(j.status)) || jobs[0] || null);
        }
      })
      .catch(e => {
        if (mounted) setError(e.message);
      });
    return () => {
      mounted = false;
    };
  }, []);

  // Poll only the active job, never simulate progress.
  useJobPolling(job?.id, job?.status, { onUpdate: setJob, onTerminal: loadHistory, onError: setError });

  const notices = <Notices error={error} onDismissError={() => setError('')} notice={scheduleMessage} />;

  return (
    <div className="app-shell">
      <Sidebar connected={connected} historyCount={history.length} />
      <main>
        <Topbar />
        <div className="main-content">
          <Routes>
            <Route
              path="/"
              element={
                <HomeRoute>
                  <ShortsPage
                    job={job}
                    setJob={setJob}
                    connected={connected}
                    loadHistory={loadHistory}
                    setError={setError}
                    notices={notices}
                    onScheduled={setScheduleMessage}
                  />
                </HomeRoute>
              }
            />
            <Route
              path="/history"
              element={<HistoryPage history={history} onRefresh={refreshHistory} onOpen={setJob} notices={notices} />}
            />
            <Route path="/story" element={<StoryStudioPage notices={notices} />} />
            <Route path="/story/:projectId" element={<StoryStudioPage notices={notices} />} />
            <Route
              path="/publish"
              element={<PublishingPage notices={notices} initialFilter={scheduleMessage ? 'queue' : 'calendar'} />}
            />
            <Route path="/library" element={<MediaLibraryPage notices={notices} />} />
            <Route path="*" element={<NotFoundPage />} />
          </Routes>
          <footer>
            JWD CREATOR STUDIO<span>Thoughtful edits. Your final say.</span>
          </footer>
        </div>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}
