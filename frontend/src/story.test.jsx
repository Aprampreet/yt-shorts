import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import StoryStudioPage from './pages/StoryStudioPage';
import { api } from './components/api';

vi.mock('./components/api', () => ({ api: vi.fn(), initialize: vi.fn(), asset: path => path }));

// ------------------------------------------------------------------ fixtures

const report = (over = {}) => ({
  score: 0.82, threshold: 0.7, passed: true, gates: { structure_complete: true, originality: true, length: true },
  components: { structure: 1, length_fit: 0.9, grounding: 0.8, originality: 1, hook: 0.6, readability: 0.7 },
  explanations: { grounding: 'Two of three numbers appear in the claims.' }, reasons: [],
  originality: { containment_5: 0.04, longest_run: 3, passed: true }, basis: 'Editorial heuristics.', ...over,
});

const script = (over = {}) => ({
  id: 's1', project_id: 'p1', concept_id: 'k1', manual: false, version: 1, title: 'The hidden door', language: 'en',
  sections: { hook: 'Nobody saw this door.', context: 'It sits behind the boss.', core: ['Point one.'], payoff: 'Now you know.', cta: '' },
  full_text: 'Nobody saw this door.', word_count: 80, estimated_duration: 33.2, quality_report: report(), attempts: 1,
  attempts_left: 5, status: 'draft', narration: null, voice: null, scenes: [], storyboard_ready: false, ...over,
});

const claim = (over = {}) => ({ id: 'c1', text: 'The door opens at level 40.', quote: 'it opens at level forty', status: 'source_claim', evidence: null, ...over });

const intelligence = (over = {}) => ({
  method: 'llm', provider: 'anthropic', summary: 'A look at the door.', sections: [{ id: 's1', title: 'Opening', units: 4 }],
  claims: [claim()], unknowns: [], duplicates: [], notes: [], text_provider_unavailable: false, provider_reasons: [], ...over,
});

const concept = (id, over = {}) => ({
  id, index: 0, angle: 'explainer', title: `Concept ${id}`, logline: 'A logline.', hook_idea: 'A hook.', key_points: ['Key one'],
  diversity_score: 0.8, quality_score: 0.7, selected: false, has_script: false, ...over,
});

const project = (over = {}) => ({
  id: 'p1', title: 'Elden Ring secrets', language: 'en', status: 'draft', video_count: 3, url: '', counts: {},
  inputs: { url: '', content_format: 'gaming_commentary', narration_style: 'natural', target_duration: '30_45', video_count: 3,
    theme: 'gaming_hype', caption_preset: 'clean_commentary', music_asset_id: null },
  source: null, transcript_chars: 1200, intelligence: {}, concepts: [], scripts: [], videos: [], active_job: null, jobs: [],
  text_provider: { available: true, provider: 'anthropic', reasons: [] },
  limits: { quality_threshold: 0.7, source_clip_max_share: 0.35, max_attempts_per_round: 3, max_total_attempts: 6 }, ...over,
});

const narration = { id: 'n1', title: 'Narration', kind: 'narration', origin: 'generated_tts', license_status: 'owned', license_label: 'Owned',
  review_required: false, file_url: '/api/v1/assets/n1/file', duration: 33, synthetic_voice: true, word_timing: 'provider', voice: { voice_id: 'Kore' } };

const scene = (over = {}) => ({
  id: 'sc1', order: 1, start: 0, end: 4.2, narration_text: 'Nobody saw this door.', visual_type: 'title_card', asset: null,
  card: { kind: 'title_card', title: 'The hidden door' }, claim_id: null, attribution: null, transition: 'cut', ...over,
});

const readyScript = (over = {}) => script({ status: 'approved', narration, scenes: [scene()], storyboard_ready: true, ...over });

const failure = (message, status = 400, data = {}) => Object.assign(new Error(message), { status, data: { message, ...data } });
const body = call => JSON.parse(call[1].body);
const called = (path, method) => api.mock.calls.filter(([p, o]) => p === path && (o?.method || 'GET') === method);

/** Routes mocked API calls by "METHOD path". Functions receive the request options. */
function mockApi(routes) {
  api.mockImplementation((path, options = {}) => {
    const key = `${options.method || 'GET'} ${path}`;
    if (key in routes) {
      const value = typeof routes[key] === 'function' ? routes[key](options) : routes[key];
      return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
    }
    if (path.startsWith('/assets')) return Promise.resolve({ total: 0, items: [] });
    if (path === '/narration/voices') return Promise.resolve({ providers: [], manual_upload: true });
    return Promise.resolve({});
  });
}

function renderStory(url = '/story/p1') {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/story" element={<StoryStudioPage />} />
        <Route path="/story/:projectId" element={<StoryStudioPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const rail = () => within(screen.getByRole('navigation', { name: 'Story steps' }));
const stepItem = name => rail().getByRole('button', { name: new RegExp(`^\\S*\\s*${name}`) }).closest('li');
const openStep = async name => {
  await screen.findByRole('navigation', { name: 'Story steps' });
  fireEvent.click(rail().getByRole('button', { name: new RegExp(name) }));
};

afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());

// ------------------------------------------------------------------ input

describe('Story input form', () => {
  async function openForm() {
    mockApi({ 'GET /story/projects': { projects: [] } });
    renderStory('/story');
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }));
    return screen.findByRole('form', { name: 'New story project' });
  }

  it('requires a URL or a transcript', async () => {
    await openForm();
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    expect(await screen.findByText(/at least one/)).toBeInTheDocument();
    expect(called('/story/projects', 'POST')).toHaveLength(0);
  });

  it('requires the rights declaration when a URL is given and says public is not free to reuse', async () => {
    await openForm();
    fireEvent.change(screen.getByLabelText('YouTube URL (optional)'), { target: { value: 'https://www.youtube.com/watch?v=abc' } });
    expect(screen.getByText(/does not make it free to reuse/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    expect(await screen.findByText('Confirm the rights declaration to use a URL.')).toBeInTheDocument();
    expect(called('/story/projects', 'POST')).toHaveLength(0);
  });

  it('shows the character count against the 60,000 limit', async () => {
    await openForm();
    fireEvent.change(screen.getByLabelText('Transcript or notes'), { target: { value: 'hello' } });
    expect(screen.getByText('5 / 60,000 characters')).toBeInTheDocument();
  });

  it('creates the project with the chosen options and opens it', async () => {
    mockApi({
      'GET /story/projects': { projects: [] },
      'POST /story/projects': project(),
      'GET /story/projects/p1': project(),
    });
    renderStory('/story');
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }));
    fireEvent.change(screen.getByLabelText('YouTube URL (optional)'), { target: { value: ' https://www.youtube.com/watch?v=abc ' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /I own this video/ }));
    fireEvent.change(screen.getByLabelText('Transcript or notes'), { target: { value: ' Notes about the door. ' } });
    fireEvent.change(screen.getByLabelText('Title (optional)'), { target: { value: 'Door' } });
    fireEvent.change(screen.getByLabelText('Language'), { target: { value: 'hinglish' } });
    fireEvent.change(screen.getByLabelText('Content format'), { target: { value: 'myth_vs_fact' } });
    fireEvent.change(screen.getByLabelText('Narration style'), { target: { value: 'documentary' } });
    fireEvent.change(screen.getByLabelText('Target duration'), { target: { value: '45_60' } });
    fireEvent.change(screen.getByLabelText('Number of videos'), { target: { value: '2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));

    await waitFor(() => expect(called('/story/projects', 'POST')).toHaveLength(1));
    expect(body(called('/story/projects', 'POST')[0])).toEqual({
      url: 'https://www.youtube.com/watch?v=abc', transcript: 'Notes about the door.', title: 'Door', language: 'hinglish',
      content_format: 'myth_vs_fact', narration_style: 'documentary', target_duration: '45_60', video_count: 2, rights_declaration: true,
    });
    expect(await screen.findByRole('navigation', { name: 'Story steps' })).toBeInTheDocument();
  });

  it('sends a transcript-only project without a URL or rights declaration', async () => {
    mockApi({ 'GET /story/projects': { projects: [] }, 'POST /story/projects': project(), 'GET /story/projects/p1': project() });
    renderStory('/story');
    fireEvent.click(await screen.findByRole('button', { name: 'New project' }));
    expect(screen.queryByRole('checkbox', { name: /I own this video/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Transcript or notes'), { target: { value: 'Just notes' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create project' }));
    await waitFor(() => expect(called('/story/projects', 'POST')).toHaveLength(1));
    const sent = body(called('/story/projects', 'POST')[0]);
    expect(sent.url).toBeNull();
    expect(sent.rights_declaration).toBe(false);
    expect(sent.video_count).toBe(3);
  });
});

// ------------------------------------------------------------------ project list

describe('Project list', () => {
  const summary = { id: 'p9', title: 'Boss notes', status: 'needs_review', content_format: 'explainer', counts: { concepts: 3, scripts: 2, videos: 1 } };

  it('shows status and counts, and deletes only after confirmation', async () => {
    let projects = [summary];
    mockApi({
      'GET /story/projects': () => ({ projects }),
      // The API answers 204 with no body; api() reports that as an unreadable response without a status.
      'DELETE /story/projects/p9': () => { projects = []; return new Error('The server returned an unreadable response.'); },
    });
    renderStory('/story');
    expect(await screen.findByText('Boss notes')).toBeInTheDocument();
    expect(screen.getByText('needs review')).toBeInTheDocument();
    expect(screen.getByText('3 concepts · 2 scripts · 1 videos')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Delete Boss notes' }));
    expect(called('/story/projects/p9', 'DELETE')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    expect(await screen.findByText('No story projects yet.')).toBeInTheDocument();
    expect(called('/story/projects/p9', 'DELETE')).toHaveLength(1);
  });

  it('shows a retryable error when the list fails', async () => {
    let fail = true;
    mockApi({ 'GET /story/projects': () => (fail ? new Error('Server down') : { projects: [summary] }) });
    renderStory('/story');
    expect(await screen.findByRole('alert')).toHaveTextContent('Server down');
    fail = false;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Boss notes')).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ resume

describe('Resuming a project', () => {
  it('a new project starts at Analyze with later steps blocked and explained', async () => {
    mockApi({ 'GET /story/projects/p1': project() });
    renderStory();
    expect(await screen.findByRole('heading', { name: 'Source intelligence' })).toBeInTheDocument();
    expect(stepItem('Source')).toHaveClass('complete');
    expect(stepItem('Analyze')).toHaveClass('available');
    expect(stepItem('Concepts')).toHaveClass('blocked');
    expect(screen.getByText('Analyze the source first')).toBeInTheDocument();
    expect(stepItem('Narration')).toHaveClass('blocked');
    expect(screen.getByText('Approve a script first', { selector: '#step-narration-why' })).toBeInTheDocument();
    expect(stepItem('Render')).toHaveClass('blocked');
  });

  it('resumes at Concepts after analysis', async () => {
    mockApi({ 'GET /story/projects/p1': project({ intelligence: intelligence(), concepts: [concept('k1')] }) });
    renderStory();
    expect(await screen.findByRole('heading', { name: 'Concepts' })).toBeInTheDocument();
    expect(stepItem('Analyze')).toHaveClass('complete');
    expect(stepItem('Concepts')).toHaveClass('available');
    expect(stepItem('Scripts')).toHaveClass('available');
  });

  it('resumes at Storyboard when an approved script has narration but no scenes', async () => {
    const draft = script({ status: 'approved', narration, storyboard_ready: false });
    mockApi({ 'GET /story/projects/p1': project({ intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [draft] }) });
    renderStory();
    expect(await screen.findByRole('heading', { name: 'Storyboard' })).toBeInTheDocument();
    expect(stepItem('Scripts')).toHaveClass('complete');
    expect(stepItem('Narration')).toHaveClass('complete');
    expect(stepItem('Storyboard')).toHaveClass('available');
    expect(stepItem('Render')).toHaveClass('blocked');
    expect(screen.getByText('Build a storyboard first')).toBeInTheDocument();
  });

  it('resumes at Narration for an approved script without audio', async () => {
    mockApi({ 'GET /story/projects/p1': project({ intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [script({ status: 'approved' })] }) });
    renderStory();
    expect(await screen.findByRole('heading', { name: 'Narration' })).toBeInTheDocument();
    expect(stepItem('Storyboard')).toHaveClass('blocked');
    expect(screen.getByText('Add narration to an approved script first')).toBeInTheDocument();
  });

  it('shows Render complete once videos exist', async () => {
    const video = { id: 'v1', short_number: 1, title: 'Door video', duration: 33, qa_status: 'passed', quality_score: 0.8,
      video_url: '/v', download_url: '/d', thumbnail_url: '/t', script_id: 's1', quality_report: {} };
    mockApi({ 'GET /story/projects/p1': project({ intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [readyScript()], videos: [video] }) });
    renderStory();
    expect(await screen.findByRole('heading', { name: 'Render' })).toBeInTheDocument();
    expect(stepItem('Render')).toHaveClass('complete');
    expect(screen.getByRole('link', { name: 'Schedule in Publishing Studio' })).toHaveAttribute('href', '/publish');
  });

  it('disables conflicting actions and offers Cancel while a job runs, using the job progress', async () => {
    const job = { id: 'j1', status: 'running', phase: 'analyzing', stage: 'Reading source', progress: 37, settings: {} };
    mockApi({
      'GET /story/projects/p1': project({ active_job: job, jobs: [{ id: 'j1', step: 'analyze', status: 'running' }] }),
      'GET /jobs/j1': job,
      'POST /jobs/j1/cancel': { ...job, status: 'cancelled' },
    });
    renderStory();
    const bar = await screen.findByRole('progressbar', { name: 'Step progress' });
    expect(bar).toHaveAttribute('value', '37');
    expect(screen.getByText('Reading source')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Analyze source' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel step' }));
    await waitFor(() => expect(called('/jobs/j1/cancel', 'POST')).toHaveLength(1));
  });

  it('shows why a missing project cannot be opened', async () => {
    mockApi({ 'GET /story/projects/p1': failure('Not Found', 404) });
    renderStory();
    expect(await screen.findByText('This story project does not exist or was deleted.')).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ intelligence

describe('Source intelligence', () => {
  const claims = [
    claim({ id: 'c1', status: 'source_claim' }),
    claim({ id: 'c2', text: 'Probably a sequel.', quote: '', status: 'inferred' }),
    claim({ id: 'c3', text: 'Boss has 5000 HP.', status: 'verified', evidence: { url: 'https://wiki.example/boss', note: 'wiki', basis: 'x' } }),
  ];

  async function open(extra = {}) {
    mockApi({
      'GET /story/projects/p1': project({ intelligence: intelligence({ claims, ...extra }) }),
      'PUT /story/projects/p1/claims/c2': claim({ id: 'c2', status: 'verified' }),
      'DELETE /story/projects/p1/claims/c3/evidence': claim({ id: 'c3', status: 'source_claim' }),
    });
    renderStory();
    await openStep('Analyze');
    return screen.findByRole('heading', { name: 'Source intelligence' });
  }

  it('labels every claim by origin, shows quotes and states nothing is fact-checked', async () => {
    await open();
    expect(screen.getByText('Stated by source')).toBeInTheDocument();
    expect(screen.getByText('Model inference')).toBeInTheDocument();
    expect(screen.getByText('Verified by you')).toBeInTheDocument();
    expect(screen.getAllByText('it opens at level forty', { selector: 'blockquote' })).toHaveLength(2);
    expect(screen.getByText(/Nothing is independently fact-checked/)).toBeInTheDocument();
  });

  it('adds evidence to a claim', async () => {
    await open();
    const form = screen.getByRole('form', { name: 'Add evidence for claim c2' });
    fireEvent.click(within(form).getByRole('button', { name: 'Save evidence' }));
    expect(within(form).getByRole('alert')).toHaveTextContent('Add a link or a note as evidence.');
    expect(called('/story/projects/p1/claims/c2', 'PUT')).toHaveLength(0);
    fireEvent.change(within(form).getByLabelText('Evidence link'), { target: { value: 'https://example.com/proof' } });
    fireEvent.change(within(form).getByLabelText('Evidence note'), { target: { value: 'Patch notes' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Save evidence' }));
    await waitFor(() => expect(called('/story/projects/p1/claims/c2', 'PUT')).toHaveLength(1));
    expect(body(called('/story/projects/p1/claims/c2', 'PUT')[0])).toEqual({ evidence_url: 'https://example.com/proof', evidence_note: 'Patch notes' });
  });

  it('removes evidence', async () => {
    await open();
    fireEvent.click(screen.getByRole('button', { name: 'Remove evidence from claim c3' }));
    await waitFor(() => expect(called('/story/projects/p1/claims/c3/evidence', 'DELETE')).toHaveLength(1));
  });

  it('explains the deterministic fallback with the provider reasons', async () => {
    await open({ method: 'deterministic', text_provider_unavailable: true, provider_reasons: ['No API key is configured.'], unknowns: ['Release date'], duplicates: [{ text: 'Same line', duplicate_of: 'x' }] });
    expect(screen.getByText(/Deterministic fallback/)).toBeInTheDocument();
    expect(screen.getByText(/No text provider is available/)).toBeInTheDocument();
    expect(screen.getByText('No API key is configured.')).toBeInTheDocument();
    expect(screen.getByText('Unknowns (1)')).toBeInTheDocument();
    expect(screen.getByText('Removed duplicates (1)')).toBeInTheDocument();
  });

  it('starts analysis and shows the job returned by the API', async () => {
    const job = { id: 'j2', status: 'queued', phase: 'queued', stage: 'Waiting for worker', progress: 0, settings: {} };
    mockApi({ 'GET /story/projects/p1': project(), 'POST /story/projects/p1/analyze': job, 'GET /jobs/j2': job });
    renderStory();
    fireEvent.click(await screen.findByRole('button', { name: 'Analyze source' }));
    expect(await screen.findByText('Waiting for worker')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: 'Step progress' })).toHaveAttribute('value', '0');
  });
});

// ------------------------------------------------------------------ concepts

describe('Concepts', () => {
  it('limits the selection to the video count and saves the chosen ids', async () => {
    const concepts = [concept('k1'), concept('k2'), concept('k3', { diversity_score: 0.2 })];
    mockApi({
      'GET /story/projects/p1': project({ video_count: 2, intelligence: intelligence(), concepts }),
      'POST /story/projects/p1/concepts/select': { concepts },
    });
    renderStory();
    await screen.findByRole('heading', { name: 'Concepts' });
    const box = id => screen.getByRole('checkbox', { name: `Select concept: Concept ${id}` });
    fireEvent.click(box('k1'));
    fireEvent.click(box('k2'));
    expect(screen.getByText('Selected 2 of 2')).toBeInTheDocument();
    expect(box('k3')).toBeDisabled();
    expect(screen.getByText(/Some concepts overlap/)).toBeInTheDocument();
    expect(screen.getAllByText(/Quality \(heuristic\)/).length).toBe(3);
    expect(screen.getByText(/not predictions of views or virality/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save selection' }));
    await waitFor(() => expect(called('/story/projects/p1/concepts/select', 'POST')).toHaveLength(1));
    expect(body(called('/story/projects/p1/concepts/select', 'POST')[0])).toEqual({ concept_ids: ['k1', 'k2'] });
  });
});

// ------------------------------------------------------------------ scripts

describe('Scripts', () => {
  const withScript = (s, extra = {}) => project({ intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [s], ...extra });

  it('saves edits with a PUT of the sections', async () => {
    mockApi({ 'GET /story/projects/p1': withScript(script()), 'PUT /story/scripts/s1': script() });
    renderStory();
    await screen.findByRole('heading', { name: 'Scripts' });
    expect(screen.getByRole('button', { name: 'Save edits' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Hook'), { target: { value: 'A sharper hook.' } });
    fireEvent.change(screen.getByLabelText('Core point 1'), { target: { value: 'Point one, revised.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save edits' }));
    await waitFor(() => expect(called('/story/scripts/s1', 'PUT')).toHaveLength(1));
    expect(body(called('/story/scripts/s1', 'PUT')[0])).toEqual({
      title: 'The hidden door',
      sections: { hook: 'A sharper hook.', context: 'It sits behind the boss.', core: ['Point one, revised.'], payoff: 'Now you know.', cta: '' },
    });
  });

  it('shows the quality report with bars, explanations and word count', async () => {
    mockApi({ 'GET /story/projects/p1': withScript(script({ quality_report: report({ score: 0.55, passed: false, reasons: ['Hook is weak'] }) })) });
    renderStory();
    await screen.findByRole('heading', { name: 'Scripts' });
    const quality = within(screen.getByRole('region', { name: 'Quality report' }));
    expect(quality.getByText('0.55')).toBeInTheDocument();
    expect(quality.getByText(/threshold 0.70/)).toBeInTheDocument();
    expect(quality.getByText('Below threshold')).toBeInTheDocument();
    expect(quality.getByText('Two of three numbers appear in the claims.')).toBeInTheDocument();
    expect(quality.getByText('Hook is weak')).toBeInTheDocument();
    expect(quality.getAllByRole('img').length).toBe(6);
    expect(screen.getByText(/80 words/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Regenerate (5 left)' })).toBeEnabled();
  });

  it('approves a passing script without an override', async () => {
    mockApi({ 'GET /story/projects/p1': withScript(script()), 'POST /story/scripts/s1/approve': script({ status: 'approved' }) });
    renderStory();
    await screen.findByRole('heading', { name: 'Scripts' });
    fireEvent.click(screen.getByRole('button', { name: 'Approve script' }));
    await waitFor(() => expect(called('/story/scripts/s1/approve', 'POST')).toHaveLength(1));
  });

  it('needs an explicit confirmation to approve below the threshold, then sends override_quality', async () => {
    const low = script({ quality_report: report({ score: 0.55, passed: false, reasons: ['Hook is weak'] }) });
    mockApi({ 'GET /story/projects/p1': withScript(low), 'POST /story/scripts/s1/approve?override_quality=true': script({ status: 'approved' }) });
    renderStory();
    await screen.findByRole('heading', { name: 'Scripts' });
    fireEvent.click(screen.getByRole('button', { name: 'Approve script' }));
    expect(called('/story/scripts/s1/approve', 'POST')).toHaveLength(0);
    const override = screen.getByRole('button', { name: 'Approve despite low score' });
    expect(override).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the quality report/ }));
    expect(override).toBeEnabled();
    fireEvent.click(override);
    await waitFor(() => expect(called('/story/scripts/s1/approve?override_quality=true', 'POST')).toHaveLength(1));
  });

  it('cannot approve a script that failed the originality gate, even by override', async () => {
    const copied = script({ quality_report: report({ score: 0.4, passed: false, gates: { structure_complete: true, originality: false, length: true }, originality: { containment_5: 0.6, longest_run: 20, passed: false } }) });
    mockApi({ 'GET /story/projects/p1': withScript(copied) });
    renderStory();
    await screen.findByRole('heading', { name: 'Scripts' });
    expect(screen.getByRole('button', { name: 'Approve script' })).toBeDisabled();
    expect(screen.getByText(/too close to the source text, so it cannot be approved, even by override/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve despite low score' })).not.toBeInTheDocument();
  });

  it('with no text provider shows the reasons and makes the manual script the main path', async () => {
    const manual = script({ id: 's2', manual: true, concept_id: null, attempts_left: null, title: 'Mine' });
    mockApi({
      'GET /story/projects/p1': project({
        intelligence: intelligence({ method: 'deterministic' }), concepts: [],
        text_provider: { available: false, provider: null, reasons: ['ANTHROPIC_API_KEY is not set.'] },
      }),
      'POST /story/projects/p1/scripts/manual': manual,
    });
    renderStory();
    await screen.findByRole('heading', { name: 'Concepts' });
    expect(screen.getByRole('button', { name: 'Generate concepts' })).toBeDisabled();
    expect(screen.getByText('ANTHROPIC_API_KEY is not set.')).toBeInTheDocument();
    await openStep('Scripts');
    expect(await screen.findByText('AI script writing is unavailable.')).toBeInTheDocument();
    expect(screen.getByText('ANTHROPIC_API_KEY is not set.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Write scripts' })).not.toBeInTheDocument();
    const form = screen.getByRole('form', { name: 'Write my own script' });
    fireEvent.click(within(form).getByRole('button', { name: 'Create script' }));
    expect(within(form).getByRole('alert')).toHaveTextContent('both required');
    fireEvent.change(within(form).getByLabelText('Script title'), { target: { value: 'Mine' } });
    fireEvent.change(within(form).getByLabelText('Script language'), { target: { value: 'hi' } });
    fireEvent.change(within(form).getByLabelText('Script text'), { target: { value: 'Line one. Line two.' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Create script' }));
    await waitFor(() => expect(called('/story/projects/p1/scripts/manual', 'POST')).toHaveLength(1));
    expect(body(called('/story/projects/p1/scripts/manual', 'POST')[0])).toEqual({ title: 'Mine', language: 'hi', full_text: 'Line one. Line two.' });
  });

  it('keeps the manual path visible when a provider is available', async () => {
    mockApi({ 'GET /story/projects/p1': withScript(script()) });
    renderStory();
    await screen.findByRole('heading', { name: 'Scripts' });
    expect(screen.getByText('Write my own script', { selector: 'summary' })).toBeVisible();
  });
});

// ------------------------------------------------------------------ narration

describe('Narration', () => {
  const approvedProject = () => project({ intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [script({ status: 'approved' })] });
  const provider = (over = {}) => ({
    name: 'gemini', label: 'Gemini TTS', available: true, reason: '', billing: 'free_tier', languages: ['multilingual'],
    commercial_use: true, notes: 'Free tier: content may be used by Google to improve products.', voices: [{ id: 'Kore', label: 'Kore' }], ...over,
  });

  it('with every provider unavailable shows reasons and the upload path, and no generate button', async () => {
    mockApi({
      'GET /story/projects/p1': approvedProject(),
      'GET /narration/voices?language=en': { manual_upload: true, providers: [
        provider({ available: false, reason: 'GEMINI_API_KEY is not set.', voices: [] }),
        provider({ name: 'elevenlabs', label: 'ElevenLabs', available: false, reason: 'Plan has no commercial rights.', voices: [], billing: 'paid', commercial_use: false, notes: '' }),
      ] },
    });
    renderStory();
    await screen.findByRole('heading', { name: 'Narration' });
    expect(await screen.findByText('GEMINI_API_KEY is not set.')).toBeInTheDocument();
    expect(screen.getByText('Plan has no commercial rights.')).toBeInTheDocument();
    expect(screen.getByText('No hosted voice provider is available right now.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Generate narration' })).not.toBeInTheDocument();
    expect(screen.getByRole('form', { name: 'Upload my own narration' })).toBeInTheDocument();
  });

  it('shows provider details verbatim and generates narration as synthetic', async () => {
    mockApi({
      'GET /story/projects/p1': approvedProject(),
      'GET /narration/voices?language=en': { manual_upload: true, providers: [provider()] },
      'POST /story/scripts/s1/narrate': script({ status: 'approved', narration }),
    });
    renderStory();
    expect(await screen.findByText('Free tier: content may be used by Google to improve products.')).toBeInTheDocument();
    expect(screen.getByText('Commercial use allowed')).toBeInTheDocument();
    expect(screen.getByText('Free tier')).toBeInTheDocument();
    expect(screen.getByText(/Voices generated here are synthetic/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/^Speed/), { target: { value: '1.1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Generate narration' }));
    await waitFor(() => expect(called('/story/scripts/s1/narrate', 'POST')).toHaveLength(1));
    expect(body(called('/story/scripts/s1/narrate', 'POST')[0])).toEqual({ provider: 'gemini', voice_id: 'Kore', speed: 1.1 });
  });

  it('plays the previewed voice and shows retry seconds on a rate limit', async () => {
    let calls = 0;
    mockApi({
      'GET /story/projects/p1': approvedProject(),
      'GET /narration/voices?language=en': { manual_upload: true, providers: [provider()] },
      'POST /narration/preview': () => {
        calls += 1;
        return calls === 1
          ? failure('Gemini is rate limited.', 429, { error: 'provider_rate_limited', retry_after_seconds: 42 })
          : { id: 'pv', file_url: '/api/v1/assets/pv/file' };
      },
    });
    renderStory();
    const button = await screen.findByRole('button', { name: 'Preview voice' });
    fireEvent.click(button);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Gemini is rate limited.');
    expect(alert).toHaveTextContent('Retry in 42 seconds');
    fireEvent.click(button);
    const audio = await screen.findByLabelText('Voice preview');
    expect(audio).toHaveAttribute('src', '/api/v1/assets/pv/file');
    expect(screen.getByText('Synthetic voice preview')).toBeInTheDocument();
    expect(body(called('/narration/preview', 'POST')[0])).toEqual({ language: 'en', voice_id: 'Kore', speed: 1, provider: 'gemini' });
  });

  it('uploads my own narration with the declaration and attaches it to the script', async () => {
    mockApi({
      'GET /story/projects/p1': approvedProject(),
      'POST /narration/upload': { id: 'up1', kind: 'narration' },
      'POST /story/scripts/s1/narration': script({ status: 'approved' }),
    });
    renderStory();
    const form = await screen.findByRole('form', { name: 'Upload my own narration' });
    fireEvent.click(within(form).getByRole('button', { name: 'Upload narration' }));
    expect(within(form).getByRole('alert')).toHaveTextContent('Choose an audio file.');
    const file = new File(['abc'], 'voice.mp3', { type: 'audio/mpeg' });
    fireEvent.change(within(form).getByLabelText('Narration audio file'), { target: { files: [file] } });
    fireEvent.click(within(form).getByRole('button', { name: 'Upload narration' }));
    expect(within(form).getByRole('alert')).toHaveTextContent('Confirm that you recorded this narration yourself.');
    fireEvent.click(within(form).getByRole('checkbox', { name: 'I recorded this myself.' }));
    fireEvent.click(within(form).getByRole('button', { name: 'Upload narration' }));
    await waitFor(() => expect(called('/story/scripts/s1/narration', 'POST')).toHaveLength(1));
    const upload = called('/narration/upload', 'POST')[0][1].body;
    expect(upload.get('i_recorded_this')).toBe('true');
    expect(upload.get('file')).toBe(file);
    expect(body(called('/story/scripts/s1/narration', 'POST')[0])).toEqual({ asset_id: 'up1' });
  });
});

// ------------------------------------------------------------------ storyboard

describe('Storyboard', () => {
  const boardProject = scenes => project({
    intelligence: intelligence(), concepts: [concept('k1', { selected: true })],
    scripts: [readyScript({ scenes })], videos: [],
  });

  it('lists scenes with time range, visual type, licence and original-graphic labels', async () => {
    const clip = scene({
      id: 'sc2', order: 2, start: 4.2, end: 10.2, visual_type: 'source_clip', transition: 'crossfade', card: null,
      asset: { id: 'a1', title: 'Boss footage', kind: 'video', license_status: 'unknown', license_label: 'Unknown', review_required: true, thumbnail_url: '/api/v1/assets/a1/thumbnail', third_party_source_footage: true },
    });
    mockApi({ 'GET /story/projects/p1': boardProject([scene(), clip]) });
    renderStory();
    await openStep('Storyboard');
    const list = within(await screen.findByRole('list', { name: 'Scenes' }));
    expect(list.getByText('Original graphic')).toBeInTheDocument();
    expect(list.getByText('Unknown · review required')).toBeInTheDocument();
    expect(list.getByText('4.2s to 10.2s')).toBeInTheDocument();
    expect(list.getByText('Source clip (third party)')).toBeInTheDocument();
    expect(list.getByAltText('Thumbnail of Boss footage')).toHaveAttribute('src', '/api/v1/assets/a1/thumbnail');
    expect(screen.getByText(/Third-party source clips: 18% of the video \(limit 35%\)/)).toBeInTheDocument();
  });

  it('offers source clips only for a URL source with a rights declaration', async () => {
    mockApi({ 'GET /story/projects/p1': boardProject([scene()]) });
    renderStory();
    await openStep('Storyboard');
    expect(await screen.findByText(/Source clips are off/)).toBeInTheDocument();
    cleanup();
    mockApi({ 'GET /story/projects/p1': { ...boardProject([scene()]), source: { url: 'https://youtu.be/x', rights_declaration: true } } });
    renderStory();
    await openStep('Storyboard');
    expect(await screen.findByRole('checkbox', { name: /Allow short clips from the source video/ })).toBeInTheDocument();
  });

  it('sends a PUT with only the changed scene fields', async () => {
    mockApi({
      'GET /story/projects/p1': boardProject([scene()]),
      'PUT /story/scenes/sc1': scene({ transition: 'crossfade' }),
    });
    renderStory();
    await openStep('Storyboard');
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scene 1' }));
    const form = screen.getByRole('form', { name: 'Edit scene 1' });
    fireEvent.change(within(form).getByLabelText('Transition'), { target: { value: 'crossfade' } });
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: 'The hidden door, revealed' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Save scene' }));
    await waitFor(() => expect(called('/story/scenes/sc1', 'PUT')).toHaveLength(1));
    expect(body(called('/story/scenes/sc1', 'PUT')[0])).toEqual({
      transition: 'crossfade', card: { kind: 'title_card', title: 'The hidden door, revealed' },
    });
  });

  it('checks card limits before sending and shows the backend validation error inline', async () => {
    mockApi({
      'GET /story/projects/p1': boardProject([scene()]),
      'PUT /story/scenes/sc1': failure('scene ends after the narration', 400, { error: 'invalid_scene' }),
    });
    renderStory();
    await openStep('Storyboard');
    fireEvent.click(await screen.findByRole('button', { name: 'Edit scene 1' }));
    const form = screen.getByRole('form', { name: 'Edit scene 1' });
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: 'x'.repeat(73) } });
    fireEvent.click(within(form).getByRole('button', { name: 'Save scene' }));
    expect(within(form).getByRole('alert')).toHaveTextContent('Title is limited to 72 characters.');
    expect(called('/story/scenes/sc1', 'PUT')).toHaveLength(0);
    fireEvent.change(within(form).getByLabelText('Title'), { target: { value: 'Short' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Save scene' }));
    expect(await within(form).findByText('scene ends after the narration')).toBeInTheDocument();
  });

  it('builds the storyboard with preferred assets', async () => {
    const image = { id: 'img1', kind: 'image', title: 'Map', origin: 'upload', license_status: 'owned', license_label: 'Owned', review_required: false };
    mockApi({
      'GET /story/projects/p1': project({ intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [script({ status: 'approved', narration })] }),
      'GET /assets?limit=100': { total: 1, items: [image] },
      'POST /story/scripts/s1/storyboard': { script_id: 's1', scenes: [scene()], report: { scenes: 1, notes: ['Scene timing is approximate.'] } },
    });
    renderStory();
    await screen.findByRole('heading', { name: 'Storyboard' });
    fireEvent.click(await screen.findByRole('checkbox', { name: /Map/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Build storyboard' }));
    await waitFor(() => expect(called('/story/scripts/s1/storyboard', 'POST')).toHaveLength(1));
    expect(body(called('/story/scripts/s1/storyboard', 'POST')[0])).toEqual({ asset_ids: ['img1'], use_source_clips: false });
    expect(await screen.findByText('Scene timing is approximate.')).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ render

describe('Render results', () => {
  const video = {
    id: 'v1', short_number: 1, title: 'The hidden door', duration: 33.1, qa_status: 'flagged', quality_score: 0.8,
    video_url: '/api/v1/shorts/v1/video', download_url: '/api/v1/shorts/v1/download', thumbnail_url: '/api/v1/shorts/v1/thumbnail',
    script_id: 's1', timeline_summary: null,
    quality_report: {
      narration: { synthetic_voice: true, disclosure: 'This video is narrated with a synthetic (AI-generated) voice.' },
      rights_review: { status: 'manual_review_required', third_party_source_footage: ['a1'], attribution: ['Photo by Ada'], synthetic_voice: true, copyright_note: 'AI rewriting is not copyright clearance.' },
      provenance: [
        { asset_id: 'a1', title: 'Boss footage', kind: 'video', license_status: 'unknown', used_as: 'scene', third_party_source_footage: true, attribution_text: '' },
        { asset_id: 'a2', title: 'Map photo', kind: 'image', license_status: 'attribution_required', used_as: 'scene', third_party_source_footage: false, attribution_text: 'Photo by Ada' },
      ],
    },
  };
  const shaping = 'Hindi (Devanagari) text cannot be rendered correctly on this machine. Install fribidi (brew install fribidi), then render again.';

  it('shows previews, provenance, the synthetic-voice disclosure, third-party flag and per-video failures', async () => {
    mockApi({
      'GET /story/projects/p1': project({
        intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [readyScript()], videos: [video],
        jobs: [{ id: 'j9', step: 'render', status: 'needs_review' }],
      }),
      'GET /jobs/j9': { id: 'j9', status: 'needs_review', analysis: { failures: [{ script_id: 's1', number: 2, error: 'text_shaping_unavailable', message: shaping }] } },
    });
    renderStory();
    await screen.findByRole('heading', { name: 'Render' });
    const preview = document.querySelector('video');
    expect(preview).toHaveAttribute('src', '/api/v1/shorts/v1/video');
    expect(screen.getByRole('link', { name: 'Download MP4' })).toHaveAttribute('href', '/api/v1/shorts/v1/download');
    const rights = within(screen.getByRole('region', { name: /Rights review for/ }));
    expect(rights.getByText('Manual review required')).toBeInTheDocument();
    expect(rights.getByText('Third-party footage')).toBeInTheDocument();
    expect(rights.getByText('Synthetic voice')).toBeInTheDocument();
    expect(rights.getByText('This video is narrated with a synthetic (AI-generated) voice.')).toBeInTheDocument();
    const used = within(rights.getByRole('list', { name: 'Assets used' }));
    expect(used.getByText('Unknown · review required')).toBeInTheDocument();
    expect(used.getByText('Attribution required')).toBeInTheDocument();
    expect(used.getByText('Credit: Photo by Ada')).toBeInTheDocument();
    expect(await screen.findByText(shaping)).toBeInTheDocument();
    expect(screen.getByText('One video could not be rendered')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Schedule in Publishing Studio' })).toHaveAttribute('href', '/publish');
  });

  it('saves the look and starts the render for the ready scripts', async () => {
    const job = { id: 'j10', status: 'queued', phase: 'queued', stage: 'Waiting for worker', progress: 0, settings: { script_ids: ['s1'] } };
    mockApi({
      'GET /story/projects/p1': project({ intelligence: intelligence(), concepts: [concept('k1', { selected: true })], scripts: [readyScript()] }),
      'PUT /story/projects/p1': project(),
      'POST /story/projects/p1/render': job,
      'GET /jobs/j10': job,
    });
    renderStory();
    await screen.findByRole('heading', { name: 'Render' });
    fireEvent.change(screen.getByLabelText('Theme'), { target: { value: 'minimal_premium' } });
    fireEvent.change(screen.getByLabelText('Caption preset'), { target: { value: 'cinematic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save look' }));
    await waitFor(() => expect(called('/story/projects/p1', 'PUT')).toHaveLength(1));
    expect(body(called('/story/projects/p1', 'PUT')[0])).toEqual({ theme: 'minimal_premium', caption_preset: 'cinematic', music_asset_id: '' });
    fireEvent.click(screen.getByRole('button', { name: 'Render videos' }));
    await waitFor(() => expect(called('/story/projects/p1/render', 'POST')).toHaveLength(1));
    expect(body(called('/story/projects/p1/render', 'POST')[0])).toEqual({ script_ids: ['s1'] });
    expect(await screen.findByText('Rendering videos')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Render videos' })).toBeDisabled();
  });
});
