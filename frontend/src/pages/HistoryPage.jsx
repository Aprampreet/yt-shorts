import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import PageHeading from '../layout/PageHeading';

/**
 * Recent projects (route `/history`). Refreshes the shell's history on mount;
 * opening a row makes it the active job and returns to the Shorts Generator.
 */
export default function HistoryPage({ history, onRefresh, onOpen, notices }) {
  const navigate = useNavigate();

  useEffect(() => {
    onRefresh();
  }, [onRefresh]);

  return (
    <>
      <PageHeading
        title="Your editing room."
        subtitle="Pick up where you left off."
        description="Reopen your generations, review the edits and download retained footage."
      />
      {notices}

      <section className="history-list">
        <div className="section-heading">
          <h2>Recent generations</h2>
          <button type="button" onClick={onRefresh}>Refresh ↻</button>
        </div>
        {history.length ? (
          history.map(item => (
            <button
              type="button"
              className="history-row"
              key={item.id}
              onClick={() => {
                onOpen(item);
                navigate('/');
              }}
            >
              <span className="history-icon" aria-hidden="true">▤</span>
              <span>
                <strong>{item.title}</strong>
                <small>
                  {new Date(item.created_at).toLocaleString()} · {item.results.length} Shorts
                </small>
              </span>
              <span className={`status-pill ${item.status}`}>{item.status.replaceAll('_', ' ')}</span>
              <span aria-hidden="true">↗</span>
            </button>
          ))
        ) : (
          <p className="empty-history">Your first project starts with a source. Create a generation to see it here.</p>
        )}
      </section>
    </>
  );
}
