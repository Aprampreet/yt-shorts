import { useLocation } from 'react-router-dom';
import { activeNavItem } from './navigation';

export default function Topbar() {
  const { pathname } = useLocation();
  const current = activeNavItem(pathname);

  return (
    <header className="topbar">
      <span>
        Workspace <b>/</b> {current ? current.label : 'Not found'}
      </span>
      <span className="version">STUDIO / 01</span>
    </header>
  );
}
