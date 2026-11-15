import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import MediaLibraryPage from './pages/MediaLibraryPage';
import { api } from './components/api';

vi.mock('./components/api', () => ({ api: vi.fn(), initialize: vi.fn(), asset: path => path }));

const base = {
  id: 'a1', kind: 'video', title: 'Boss fight', origin: 'upload', mime_type: 'video/mp4', size_bytes: 2048, duration: 75,
  width: 1920, height: 1080, license_status: 'owned', license_label: 'Owned', review_required: false, attribution_text: '',
  license_reference: '', rights_notes: '', source_url: '', tags: ['boss'], pinned: false, created_at: '2026-01-01T00:00:00Z',
  usage: { scenes: 0, projects: 0, narrations: 0 }, file_url: '/api/v1/assets/a1/file', thumbnail_url: '/api/v1/assets/a1/thumbnail',
};
const music = {
  ...base, id: 'a2', kind: 'music', title: 'Theme', license_status: 'unknown', license_label: 'Unknown', review_required: true,
  thumbnail_url: null, file_url: '/api/v1/assets/a2/file',
};

function listOf(items) {
  return { total: items.length, limit: 24, offset: 0, items };
}

function wrap(ui) {
  return render(ui);
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  api.mockImplementation(path => (path.startsWith('/assets') ? Promise.resolve(listOf([base, music])) : Promise.resolve({})));
});

describe('Media Library', () => {
  it('renders assets with thumbnails, glyphs and licence badges', async () => {
    wrap(<MediaLibraryPage />);
    expect(await screen.findByText('Boss fight')).toBeInTheDocument();
    expect(document.querySelector('img[src="/api/v1/assets/a1/thumbnail"]')).not.toBeNull();
    const grid = within(screen.getByRole('list', { name: 'Assets' }));
    expect(grid.getByText('Owned')).toBeInTheDocument();
    expect(grid.getByText('Unknown · review required')).toBeInTheDocument();
    expect(screen.getByText('Showing 1–2 of 2')).toBeInTheDocument();
  });

  it('shows an error with retry when the list fails', async () => {
    api.mockRejectedValueOnce(new Error('Server down'));
    wrap(<MediaLibraryPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Server down');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Boss fight')).toBeInTheDocument();
  });

  it('re-queries with the right params when filters change', async () => {
    wrap(<MediaLibraryPage />);
    await screen.findByText('Boss fight');
    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'audio' } });
    await waitFor(() => expect(api).toHaveBeenCalledWith('/assets?kind=audio&limit=24&offset=0'));
    fireEvent.change(screen.getByLabelText('Licence'), { target: { value: 'restricted' } });
    await waitFor(() => expect(api).toHaveBeenCalledWith('/assets?kind=audio&license_status=restricted&limit=24&offset=0'));
    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'boss' } });
    fireEvent.change(screen.getByLabelText('Tag'), { target: { value: 'Intro' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/assets?q=boss&kind=audio&license_status=restricted&tag=intro&limit=24&offset=0'));
  });

  it('paginates with offset', async () => {
    api.mockImplementation(path => Promise.resolve(path.startsWith('/assets') ? { total: 50, limit: 24, offset: 0, items: [base] } : {}));
    wrap(<MediaLibraryPage />);
    await screen.findByText('Boss fight');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/assets?limit=24&offset=24'));
  });

  it('requires a licence and attribution text before uploading', async () => {
    wrap(<MediaLibraryPage />);
    await screen.findByText('Boss fight');
    fireEvent.click(screen.getByRole('button', { name: 'Upload asset' }));
    const form = screen.getByRole('form', { name: 'Upload an asset' });
    const file = new File(['abc'], 'clip.mp4', { type: 'video/mp4' });
    fireEvent.change(within(form).getByLabelText('File'), { target: { files: [file] } });
    fireEvent.click(within(form).getByRole('button', { name: 'Upload to library' }));
    expect(await within(form).findByText('Choose a licence status. Nothing is assumed.')).toBeInTheDocument();
    expect(api).not.toHaveBeenCalledWith('/assets', expect.anything());

    fireEvent.change(within(form).getByLabelText('Licence status (required)'), { target: { value: 'attribution_required' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Upload to library' }));
    expect(await within(form).findByText(/Attribution text is required/)).toBeInTheDocument();
    expect(api).not.toHaveBeenCalledWith('/assets', expect.anything());

    api.mockImplementation((path, options) => {
      if (path === '/assets' && options?.method === 'POST') return Promise.resolve({ ...base, id: 'new', title: 'clip', deduplicated: false });
      return Promise.resolve(listOf([base]));
    });
    fireEvent.change(within(form).getByLabelText('Attribution text'), { target: { value: 'By Ann' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Upload to library' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/assets', expect.objectContaining({ method: 'POST', body: expect.any(FormData) })));
    const body = api.mock.calls.find(([path, o]) => path === '/assets' && o?.method === 'POST')[1].body;
    expect(body.get('license_status')).toBe('attribution_required');
    expect(body.get('attribution_text')).toBe('By Ann');
    expect(body.get('kind')).toBe('video');
    expect(body.get('file')).toBeInstanceOf(File);
    expect(await screen.findByText(/was added to your library/)).toBeInTheDocument();
  });

  it('reports a deduplicated upload as already in the library', async () => {
    wrap(<MediaLibraryPage />);
    await screen.findByText('Boss fight');
    fireEvent.click(screen.getByRole('button', { name: 'Upload asset' }));
    const form = screen.getByRole('form', { name: 'Upload an asset' });
    api.mockImplementation((path, options) => (options?.method === 'POST'
      ? Promise.resolve({ ...base, deduplicated: true })
      : Promise.resolve(listOf([base]))));
    fireEvent.change(within(form).getByLabelText('File'), { target: { files: [new File(['abc'], 'clip.mp4', { type: 'video/mp4' })] } });
    fireEvent.change(within(form).getByLabelText('Licence status (required)'), { target: { value: 'owned' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Upload to library' }));
    expect(await screen.findByText(/already in your library/)).toBeInTheDocument();
  });

  it('opens the drawer with a player and lists projects when deletion is refused with 409', async () => {
    wrap(<MediaLibraryPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Open Boss fight' }));
    const drawer = screen.getByRole('dialog');
    expect(drawer.querySelector('video')).toHaveAttribute('src', '/api/v1/assets/a1/file');

    const conflict = Object.assign(new Error('This asset is used in: Launch video. Remove it first.'), {
      status: 409,
      data: { error: 'asset_in_use', projects: [{ id: 'p1', title: 'Launch video' }] },
    });
    api.mockRejectedValueOnce(conflict);
    fireEvent.click(within(drawer).getByRole('button', { name: 'Delete asset' }));
    expect(api).not.toHaveBeenCalledWith('/assets/a1', expect.objectContaining({ method: 'DELETE' }));
    fireEvent.click(within(drawer).getByRole('button', { name: 'Confirm delete' }));
    expect(await within(drawer).findByText('Launch video', { selector: 'li' })).toBeInTheDocument();
    expect(drawer).toHaveTextContent('cannot be deleted');
  });

  it('plays audio assets with an audio element', async () => {
    wrap(<MediaLibraryPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Open Theme' }));
    expect(screen.getByRole('dialog').querySelector('audio')).not.toBeNull();
  });
});
