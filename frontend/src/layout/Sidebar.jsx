import { Link, useLocation, useNavigate } from 'react-router-dom';
import { NAV_ITEMS } from './navigation';

/**
 * Persistent sidebar. Nav items are <button>s (tests query them by role) that
 * route with useNavigate and expose aria-current="page" on the active item.
 * On narrow screens the same markup collapses into a top bar via CSS.
 */
export default function Sidebar({ connected, historyCount }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  return (
    <aside className="sidebar">
      <Link to="/" className="brand" aria-label="JWD Creator Studio home">
        <span className="brand-mark" aria-hidden="true">J</span>
        <span>
          JWD
          <span className="brand-sub">CREATOR STUDIO</span>
        </span>
      </Link>

      <div className="workspace-label">WORKSPACE</div>
      <nav aria-label="Main navigation">
        {NAV_ITEMS.map(item => {
          const active = item.match(pathname);
          return (
            <button
              key={item.key}
              type="button"
              className={active ? 'selected' : ''}
              aria-current={active ? 'page' : undefined}
              onClick={() => navigate(item.to)}
            >
              <span aria-hidden="true">{item.icon}</span>
              {item.label}
              {item.key === 'history' && <span className="history-count">{historyCount}</span>}
            </button>
          );
        })}
      </nav>

      <div className="sidebar-bottom">
        <div className="local-badge">
          <i className={connected ? 'online' : ''} />
          {connected ? 'Studio connected' : 'API disconnected'}
        </div>
        <p>
          Hosted intelligence.
          <br />
          Your footage stays in your studio.
        </p>
        <div className="profile">
          <span>Y</span>
          <div>
            Your workspace
            <small>Local creator studio</small>
          </div>
        </div>
      </div>
    </aside>
  );
}
