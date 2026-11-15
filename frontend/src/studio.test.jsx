import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import App from './App';
import InputCard from './components/input_card';
import ProgressBar from './components/progressbar';
import OutputCard from './components/output_card';
import { api, initialize } from './components/api';
vi.mock('./components/api', () => ({ api: vi.fn(), initialize: vi.fn(), asset: path => path }));
afterEach(cleanup);
beforeEach(() => window.history.pushState({}, '', '/'));
beforeEach(() => { vi.clearAllMocks(); initialize.mockResolvedValue(); api.mockResolvedValue([]); });
const short = { id:'short1', short_number:1,start_time:0,end_time:30,duration:30,title:'A real moment',description:'Source attribution',hashtags:['#Shorts'],quality_score:92,quality_report:{highlight_score:70,unverified:['Ownership']},video_url:'/video',thumbnail_url:'/thumb',download_url:'/download',caption_url:'/captions' };
const job = { id:'job1',status:'rendering',stage:'Rendering Short 1 of 3',progress:48,results:[],analysis:{} };

describe('Studio workflow', () => {
  it('submits the URL, permission declaration and editing options', () => {
    const submit = vi.fn(); render(<InputCard onSubmit={submit} busy={false} connected />);
    fireEvent.change(screen.getByLabelText('Start with a video'),{target:{value:'https://youtu.be/abcdefghijk'}});
    fireEvent.click(screen.getByRole('checkbox',{name:/I own this footage/}));
    fireEvent.click(screen.getByRole('button',{name:/Generate 3 Shorts/}));
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({url:'https://youtu.be/abcdefghijk',rights_declaration:true,options:expect.objectContaining({target_duration:30})}));
  });
  it('requires the rights declaration and disables repeated submission', () => {
    render(<InputCard onSubmit={vi.fn()} busy connected />);
    expect(screen.getByRole('checkbox',{name:/I own this footage/})).toBeRequired();
    expect(screen.getByRole('button',{name:/Studio is working/})).toBeDisabled();
    expect(screen.getByLabelText('Start with a video')).toBeDisabled();
  });
  it('renders actual progress and supports cancellation', () => {
    const cancel = vi.fn(); render(<ProgressBar job={job} onCancel={cancel} />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('value','48');
    expect(screen.getByText('Rendering Short 1 of 3')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button',{name:'Cancel generation'})); expect(cancel).toHaveBeenCalledOnce();
  });
  it('shows submission errors from the server', async () => {
    api.mockImplementation(path => path === '/jobs' ? Promise.resolve([]) : Promise.resolve({}));
    render(<App />);
    await waitFor(() => expect(screen.getByRole('button',{name:/Generate 3 Shorts/})).toBeEnabled());
    api.mockRejectedValueOnce(new Error('Use a public HTTPS YouTube video'));
    fireEvent.change(screen.getByLabelText('Start with a video'),{target:{value:'https://example.com/video'}});
    fireEvent.click(screen.getByRole('checkbox',{name:/I own this footage/}));
    fireEvent.click(screen.getByRole('button',{name:/Generate 3 Shorts/}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Use a public HTTPS YouTube video');
  });
  it('shows failed jobs after submission and offers retry', async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByRole('button',{name:/Generate 3 Shorts/})).toBeEnabled());
    api.mockResolvedValueOnce({...job,status:'failed',stage:'Worker unavailable',error_message:'Start Redis and the Celery worker.'});
    fireEvent.change(screen.getByLabelText('Start with a video'),{target:{value:'https://youtu.be/abcdefghijk'}});
    fireEvent.click(screen.getByRole('checkbox',{name:/I own this footage/}));
    fireEvent.click(screen.getByRole('button',{name:/Generate 3 Shorts/}));
    expect(await screen.findByRole('alert')).toHaveTextContent('Start Redis');
    expect(screen.getByRole('button',{name:/Retry generation/})).toBeInTheDocument();
  });
  it('opens a history job with three real preview cards and batch download', async () => {
    api.mockResolvedValue([{...job,status:'needs_review',title:'Past project',created_at:new Date().toISOString(),results:[short,{...short,id:'short2',short_number:2},{...short,id:'short3',short_number:3}]}]);
    render(<App />);
    await waitFor(() => expect(screen.getByRole('button',{name:/Generate 3 Shorts/})).toBeEnabled());
    fireEvent.click(screen.getByRole('button',{name:/Recent projects/}));
    fireEvent.click(await screen.findByRole('button',{name:/Past project/}));
    expect(screen.getAllByRole('link',{name:/Download MP4/})).toHaveLength(3);
    expect(screen.getByRole('link',{name:/Download all/})).toHaveAttribute('href','/api/v1/jobs/job1/download');
  });
  it('offers downloads and reports metadata regeneration errors', async () => {
    api.mockRejectedValueOnce(new Error('Hosted editorial analysis is not configured'));
    const regenerate = vi.fn(); render(<OutputCard short={short} onUpdated={vi.fn()} onRegenerate={regenerate} />);
    expect(screen.getByRole('link',{name:/Download MP4/})).toHaveAttribute('href','/download');
    expect(screen.getByRole('link',{name:/SRT captions/})).toHaveAttribute('href','/captions');
    fireEvent.click(screen.getByRole('button',{name:'Regenerate metadata'}));
    expect(await screen.findByRole('status')).toHaveTextContent('Hosted editorial analysis is not configured');
    fireEvent.click(screen.getByRole('button',{name:'Regenerate job'})); expect(regenerate).toHaveBeenCalledOnce();
  });
});

it('shows the scheduling dashboard and honest account state', async () => {
  api.mockImplementation(path => {
    if (path === '/jobs' || path === '/social/posts' || path === '/social/accounts') return Promise.resolve([]);
    if (path === '/social/preferences') return Promise.resolve({timezone:'Asia/Kolkata',slots:['12:00','17:00','21:00']});
    if (path === '/social/suggestions') return Promise.resolve([]);
    return Promise.resolve({});
  });
  render(<App />);
  await waitFor(() => expect(screen.getByRole('button',{name:/Generate 3 Shorts/})).toBeEnabled());
  fireEvent.click(screen.getByRole('button',{name:/Schedule/}));
  expect(await screen.findByText('No accounts connected.')).toBeInTheDocument();
  expect(screen.getByRole('button',{name:/Connect YouTube/})).toBeInTheDocument();
  expect(screen.getByText(/Instagram scheduling needs an always-on worker/)).toBeInTheDocument();
});

it('shows transcript edit controls for timed bilingual segments', () => {
  render(<OutputCard short={{...short,segments:[{id:'seg',start_ms:200,end_ms:1200,original_text:'Hello',translated_english:'Hello',translated_hindi:'नमस्ते',detected_language:'mixed'}]}} onUpdated={vi.fn()} onRegenerate={vi.fn()} onSchedule={vi.fn()} />);
  fireEvent.click(screen.getByText('Review & edit transcript'));
  expect(screen.getByDisplayValue('नमस्ते')).toBeInTheDocument();
  expect(screen.getByRole('button',{name:'Save & re-render captions'})).toBeInTheDocument();
});
