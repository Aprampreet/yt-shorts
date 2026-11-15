import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { MemoryRouter } from 'react-router-dom';
import PublishingPage from './pages/PublishingPage';
import PostMetadataPanel from './components/publishing/PostMetadataPanel';
import { toOffsetISO } from './components/publishing/datetime';
import { api } from './components/api';

vi.mock('./components/api', () => ({ api: vi.fn(), initialize: vi.fn(), asset: path => path }));

const platforms = {
  youtube: { configured: true, connected: true, can_publish: true, reason: null },
  instagram: { configured: false, connected: false, can_publish: false, reason: 'Set INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET.' },
};
const short = {
  id: 'short1', short_number: 1, title: 'A real moment', description: 'Desc', hashtags: ['#Shorts'], duration: 30,
  rights_review_status: 'rights_documented',
};
const jobs = [{ id: 'j1', status: 'completed', title: 'My stream', results: [short] }];

function mock(overrides = {}) {
  api.mockImplementation((path, options) => {
    if (path in overrides) return Promise.resolve(typeof overrides[path] === 'function' ? overrides[path](options) : overrides[path]);
    if (path === '/social/platforms') return Promise.resolve(platforms);
    if (path === '/jobs') return Promise.resolve(jobs);
    if (path === '/social/accounts') return Promise.resolve([{ id: 'acc1', platform: 'youtube', account_name: 'My Channel' }]);
    if (path === '/social/preferences') return Promise.resolve({ timezone: 'Asia/Kolkata', slots: ['12:00'] });
    if (path.startsWith('/social/')) return Promise.resolve([]);
    return Promise.resolve({});
  });
}

const renderPage = (url = '/publish') => render(<MemoryRouter initialEntries={[url]}><PublishingPage /></MemoryRouter>);

afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); mock(); });

describe('Publishing Studio', () => {
  it('shows the backend reason for an unconfigured Instagram and keeps the worker sentence', async () => {
    renderPage();
    expect(await screen.findByText('Set INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET.')).toBeInTheDocument();
    expect(screen.getByText(/Instagram scheduling needs an always-on worker/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect Instagram' })).not.toBeInTheDocument();
  });

  it('offers Connect Instagram only when it is configured and not connected', async () => {
    mock({ '/social/platforms': { ...platforms, instagram: { configured: true, connected: false, can_publish: false, reason: 'Connect an Instagram professional account.' } } });
    renderPage();
    expect(await screen.findByRole('button', { name: 'Connect Instagram' })).toBeInTheDocument();
    expect(screen.queryByText(/Instagram scheduling needs an always-on worker/)).not.toBeInTheDocument();
  });

  it('shows a connected notice from the URL', async () => {
    renderPage('/publish?connected=instagram');
    expect(await screen.findByText('Instagram connected')).toBeInTheDocument();
  });

  it('creates a post with an ISO datetime that has an offset, then approves it', async () => {
    const created = { id: 'p1', short_id: 'short1', platform: 'youtube', title: 'A real moment', status: 'draft' };
    mock({
      '/social/posts': options => (options?.method === 'POST' ? created : []),
      '/social/posts/p1/approve': { ...created, status: 'queued' },
    });
    renderPage();
    const select = await screen.findByLabelText('Finished video');
    await screen.findByRole('option', { name: /My stream/ });
    fireEvent.change(select, { target: { value: 'short1' } });
    fireEvent.change(screen.getByLabelText('Connected account'), { target: { value: 'acc1' } });
    fireEvent.change(screen.getByLabelText('Publish at (optional)'), { target: { value: '2999-01-02T18:30' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/social/posts', expect.objectContaining({ method: 'POST' })));
    const call = api.mock.calls.find(([path, o]) => path === '/social/posts' && o?.method === 'POST');
    const body = JSON.parse(call[1].body);
    expect(body).toMatchObject({ short_id: 'short1', platform: 'youtube', account_id: 'acc1', title: 'A real moment' });
    expect(body.scheduled_at).toMatch(/^2999-01-02T18:30:00[+-]\d\d:\d\d$/);
    expect(body.scheduled_at).toBe(toOffsetISO('2999-01-02T18:30'));

    fireEvent.click(await screen.findByRole('button', { name: 'Approve for publishing' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/social/posts/p1/approve', { method: 'POST' }));
    expect(await screen.findByText('Approved and queued. It is not published until the platform confirms.')).toBeInTheDocument();
    expect(document.querySelector('.created-post .badge')).toHaveTextContent('Queued');
  });

  it('rejects a time less than ten minutes away before calling the API', async () => {
    renderPage();
    const select = await screen.findByLabelText('Finished video');
    await screen.findByRole('option', { name: /My stream/ });
    fireEvent.change(select, { target: { value: 'short1' } });
    fireEvent.change(screen.getByLabelText('Connected account'), { target: { value: 'acc1' } });
    fireEvent.change(screen.getByLabelText('Publish at (optional)'), { target: { value: '2000-01-01T10:00' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }));
    expect(await screen.findByText('Pick a time at least ten minutes from now.')).toBeInTheDocument();
    expect(api).not.toHaveBeenCalledWith('/social/posts', expect.objectContaining({ method: 'POST' }));
  });

  it('documents the rights review with two confirmations via query params', async () => {
    mock({
      '/social/posts': options => (options?.method === 'POST' ? { id: 'p1', short_id: 'short1', platform: 'youtube', title: 't', status: 'draft' } : []),
      '/jobs': [{ ...jobs[0], results: [{ ...short, rights_review_status: '' }] }],
      '/shorts/short1/rights-review?permission_verified=true&music_verified=true': { ...short, rights_review_status: 'rights_documented' },
    });
    renderPage();
    const select = await screen.findByLabelText('Finished video');
    await screen.findByRole('option', { name: /My stream/ });
    fireEvent.change(select, { target: { value: 'short1' } });
    fireEvent.change(screen.getByLabelText('Connected account'), { target: { value: 'acc1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }));
    const review = await screen.findByRole('form', { name: 'Rights review' });
    const submit = within(review).getByRole('button', { name: 'Document rights review' });
    expect(submit).toBeDisabled();
    const [permission, music] = within(review).getAllByRole('checkbox');
    fireEvent.click(permission);
    expect(submit).toBeDisabled();
    fireEvent.click(music);
    fireEvent.click(submit);
    expect(await screen.findByText('Rights review documented')).toBeInTheDocument();
  });
});

describe('PostMetadataPanel', () => {
  const youtubePost = { id: 'p1', platform: 'youtube', status: 'draft', short_id: 'short1' };
  const youtubeMeta = {
    platform: 'youtube', source: 'saved', limits: { title_max: 100, hashtags_max: 5 }, required_attribution: ['Credit: Ann'],
    metadata: { title: 'Hello', description: 'World', hashtags: ['#a'], altered_content: false },
  };

  it('enforces the YouTube title limit client-side and sends a PUT', async () => {
    api.mockImplementation((path, options) => {
      if (options?.method === 'PUT') return Promise.resolve(youtubePost);
      return Promise.resolve(youtubeMeta);
    });
    render(<PostMetadataPanel post={youtubePost} />);
    const title = await screen.findByLabelText('Title');
    expect(screen.getByText('Credit: Ann')).toBeInTheDocument();
    fireEvent.change(title, { target: { value: 'x'.repeat(101) } });
    expect(screen.getByRole('button', { name: 'Save metadata' })).toBeDisabled();
    expect(screen.getByText(/Titles are limited to 100 characters/)).toBeInTheDocument();
    fireEvent.change(title, { target: { value: 'New title' } });
    fireEvent.change(screen.getByLabelText(/Hashtags/), { target: { value: 'one, #two' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /altered or synthetic/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Save metadata' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/social/posts/p1/metadata', expect.objectContaining({ method: 'PUT' })));
    const put = api.mock.calls.find(([, o]) => o?.method === 'PUT')[1];
    expect(JSON.parse(put.body)).toEqual({ title: 'New title', description: 'World', hashtags: ['#one', '#two'], altered_content: true });
  });

  it('shows the Instagram counter, blocks over-long captions and too many hashtags', async () => {
    const post = { id: 'p2', platform: 'instagram', status: 'draft', short_id: 's' };
    api.mockResolvedValue({
      platform: 'instagram', source: 'extractive', limits: { caption_max: 2200, hashtags_max: 30 }, required_attribution: [],
      metadata: { caption: 'Hi', hashtags: [], question: '', share_to_feed: true },
    });
    render(<PostMetadataPanel post={post} />);
    const caption = await screen.findByLabelText('Caption');
    expect(screen.getByText('2 / 2200')).toBeInTheDocument();
    fireEvent.change(caption, { target: { value: 'y'.repeat(2201) } });
    expect(screen.getByText('2201 / 2200')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save metadata' })).toBeDisabled();
    fireEvent.change(caption, { target: { value: 'ok' } });
    fireEvent.change(screen.getByLabelText(/Hashtags/), { target: { value: Array.from({ length: 31 }, (_, i) => `t${i}`).join(' ') } });
    expect(screen.getByText('At most 30 hashtags are allowed.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save metadata' })).toBeDisabled();
  });

  it('regenerates with ?regenerate=true and is read-only once queued', async () => {
    api.mockResolvedValue(youtubeMeta);
    render(<PostMetadataPanel post={youtubePost} />);
    await screen.findByLabelText('Title');
    fireEvent.click(screen.getByRole('button', { name: 'Regenerate' }));
    await waitFor(() => expect(api).toHaveBeenCalledWith('/social/posts/p1/metadata?regenerate=true'));
    cleanup();
    render(<PostMetadataPanel post={{ ...youtubePost, id: 'p3', status: 'queued' }} />);
    expect(await screen.findByLabelText('Title')).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Save metadata' })).not.toBeInTheDocument();
  });
});
