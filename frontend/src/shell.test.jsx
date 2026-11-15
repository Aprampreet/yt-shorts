import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import App from './App';
import { api, initialize } from './components/api';

vi.mock('./components/api', () => ({ api: vi.fn(), initialize: vi.fn(), asset: path => path }));

function mockApi() {
  api.mockImplementation(path => {
    if (path === '/social/preferences') return Promise.resolve({ timezone: 'Asia/Kolkata', slots: ['12:00'] });
    if (path === '/jobs' || path.startsWith('/social/')) return Promise.resolve([]);
    return Promise.resolve({});
  });
}

function renderAt(url) {
  window.history.pushState({}, '', url);
  return render(<App />);
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  initialize.mockResolvedValue();
  mockApi();
});

describe('App shell routes', () => {
  it('renders the Shorts Generator at /', async () => {
    renderAt('/');
    await waitFor(() => expect(screen.getByRole('button', { name: /Generate 3 Shorts/ })).toBeEnabled());
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Good footage.');
  });

  it('renders Recent projects at /history', async () => {
    renderAt('/history');
    expect(await screen.findByRole('heading', { name: 'Recent generations' })).toBeInTheDocument();
    await waitFor(() => expect(api).toHaveBeenCalledWith('/jobs'));
  });

  it('renders AI Story Studio at /story with an honest empty project list', async () => {
    api.mockImplementation(path => (path === '/story/projects' ? Promise.resolve({ projects: [] }) : Promise.resolve([])));
    renderAt('/story');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('From an idea');
    expect(await screen.findByText('No story projects yet.')).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith('/story/projects');
    expect(screen.getByRole('button', { name: 'New project' })).toBeInTheDocument();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(document.querySelector('video')).toBeNull();
  });

  it('shows an honest error for /story/:projectId when the project does not exist', async () => {
    api.mockImplementation(path => {
      if (path === '/story/projects/abc123') return Promise.reject(Object.assign(new Error('Not Found'), { status: 404 }));
      return Promise.resolve([]);
    });
    renderAt('/story/abc123');
    expect(await screen.findByText('This project could not be opened.')).toBeInTheDocument();
    expect(screen.getByText('This story project does not exist or was deleted.')).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith('/story/projects/abc123');
  });

  it('renders the Publishing Studio with the existing scheduler at /publish', async () => {
    renderAt('/publish');
    expect(await screen.findByText('No accounts connected.')).toBeInTheDocument();
    expect(screen.getByText(/Instagram scheduling needs an always-on worker/)).toBeInTheDocument();
  });

  it('renders the Media Library at /library with an honest empty state', async () => {
    renderAt('/library');
    expect(await screen.findByText('Your library is empty.')).toBeInTheDocument();
    expect(api).toHaveBeenCalledWith('/assets?limit=24&offset=0');
    expect(document.querySelector('video, img')).toBeNull();
  });

  it('shows a not-found page for unknown routes', () => {
    renderAt('/nope');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('This page does not exist.');
  });
});

describe('Navigation', () => {
  it('lists the main tabs in order', () => {
    renderAt('/');
    const nav = screen.getByRole('navigation', { name: 'Main navigation' });
    const labels = Array.from(nav.querySelectorAll('button')).map(b => b.textContent);
    expect(labels).toEqual(['▣Create Shorts', '◇AI Story Studio', '▦Publish & Schedule', '▤Media Library', '◷Recent projects0']);
  });

  it('marks the active item with aria-current and moves it on navigation', async () => {
    renderAt('/');
    const shorts = screen.getByRole('button', { name: /Create Shorts/ });
    expect(shorts).toHaveAttribute('aria-current', 'page');
    fireEvent.click(screen.getByRole('button', { name: /AI Story Studio/ }));
    expect(window.location.pathname).toBe('/story');
    expect(screen.getByRole('button', { name: /AI Story Studio/ })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: /Create Shorts/ })).not.toHaveAttribute('aria-current');
    fireEvent.click(screen.getByRole('button', { name: /Media Library/ }));
    expect(window.location.pathname).toBe('/library');
    expect(screen.getByRole('button', { name: /Media Library/ })).toHaveAttribute('aria-current', 'page');
  });

  it('keeps AI Story Studio active on a project route', () => {
    renderAt('/story/p1');
    expect(screen.getByRole('button', { name: /AI Story Studio/ })).toHaveAttribute('aria-current', 'page');
  });

  it('shows the product name in the sidebar and footer', () => {
    renderAt('/');
    expect(screen.getByRole('link', { name: 'JWD Creator Studio home' })).toHaveAttribute('href', '/');
    expect(screen.getByText('JWD CREATOR STUDIO')).toBeInTheDocument();
  });
});

describe('Legacy URLs', () => {
  it('redirects /?view=schedule&connected=youtube to /publish keeping the query', async () => {
    renderAt('/?view=schedule&connected=youtube');
    await waitFor(() => expect(window.location.pathname).toBe('/publish'));
    expect(window.location.search).toBe('?connected=youtube');
    expect(screen.getByRole('button', { name: /Publish & Schedule/ })).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByText('No accounts connected.')).toBeInTheDocument();
  });

  it('leaves other query strings on the Shorts Generator', () => {
    renderAt('/?view=studio');
    expect(window.location.pathname).toBe('/');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Good footage.');
  });
});
