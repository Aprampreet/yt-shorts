import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import InputCard from './components/input_card';
import OutputCard from './components/output_card';
import ProviderStatus from './components/shorts/ProviderStatus';
import { api } from './components/api';

vi.mock('./components/api', () => ({ api: vi.fn(), initialize: vi.fn(), asset: path => path }));

const short = {
  id: 'short1', short_number: 1, start_time: 0, end_time: 30, duration: 30, title: 'A real moment', description: 'd',
  hashtags: ['#Shorts'], quality_score: 92, quality_report: { highlight_score: 70 }, video_url: '/v', thumbnail_url: '/t',
  download_url: '/d', caption_url: null,
};

afterEach(cleanup);
beforeEach(() => { vi.clearAllMocks(); api.mockResolvedValue({ items: [] }); });

function fillAndSubmit(submit) {
  render(<InputCard onSubmit={submit} busy={false} connected />);
  fireEvent.change(screen.getByLabelText('Start with a video'), { target: { value: 'https://youtu.be/abcdefghijk' } });
  fireEvent.click(screen.getByRole('checkbox', { name: /I own this footage/ }));
}

describe('Editing controls', () => {
  it('sends the new defaults in a superset payload', () => {
    const submit = vi.fn();
    fillAndSubmit(submit);
    fireEvent.click(screen.getByRole('button', { name: /Generate 3 Shorts/ }));
    expect(submit.mock.calls[0][0].options).toMatchObject({
      target_duration: 30, preset: 'none', composition: 'auto', caption_preset: 'auto', hook_overlay: false,
      remove_dead_air: false, punch_ins: false, content_category: 'gaming', facecam_corner: 'top-right', secondary_asset_id: null,
    });
  });

  it('adds preset, composition and toggles to the submitted options', () => {
    const submit = vi.fn();
    fillAndSubmit(submit);
    fireEvent.click(screen.getByRole('radio', { name: /Gaming Hype/ }));
    fireEvent.change(screen.getByLabelText('Composition'), { target: { value: 'subject_follow' } });
    fireEvent.change(screen.getByLabelText('Caption preset'), { target: { value: 'bold_gaming' } });
    fireEvent.change(screen.getByLabelText('Content category'), { target: { value: 'commentary' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /hook line/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Remove dead air/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /Punch-in/ }));
    expect(screen.getByText(/not face detection/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Generate 3 Shorts/ }));
    expect(submit.mock.calls[0][0].options).toMatchObject({
      preset: 'gaming_hype', composition: 'subject_follow', caption_preset: 'bold_gaming', content_category: 'commentary',
      hook_overlay: true, remove_dead_air: true, punch_ins: true, secondary_asset_id: null,
    });
  });

  it('offers five presets plus Classic', () => {
    render(<InputCard onSubmit={vi.fn()} busy={false} connected />);
    expect(screen.getAllByRole('radio')).toHaveLength(6);
    expect(screen.getByRole('radio', { name: /Classic \(no preset\)/ })).toBeChecked();
  });

  it('shows the library video picker only for split-screen, with the licence badge and rights warning', async () => {
    api.mockResolvedValue({
      total: 1, items: [{ id: 'asset-9', title: 'Facecam loop', license_status: 'unknown', license_label: 'Unknown', review_required: true }],
    });
    const submit = vi.fn();
    fillAndSubmit(submit);
    expect(screen.queryByLabelText('Second video (library)')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: /Split-screen Gaming/ }));
    const picker = await screen.findByLabelText('Second video (library)');
    expect(api).toHaveBeenCalledWith('/assets?kind=video&limit=100');
    await screen.findByRole('option', { name: 'Facecam loop' });
    fireEvent.change(picker, { target: { value: 'asset-9' } });
    expect(screen.getByText('Unknown · review required')).toBeInTheDocument();
    expect(screen.getByText(/does not clear rights for your first video/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Generate 3 Shorts/ }));
    expect(submit.mock.calls[0][0].options).toMatchObject({ preset: 'split_screen_gaming', secondary_asset_id: 'asset-9' });
  });

  it('drops a stale secondary asset when split-screen is switched off', async () => {
    api.mockResolvedValue({ items: [{ id: 'asset-9', title: 'Loop', license_status: 'owned', license_label: 'Owned', review_required: false }] });
    const submit = vi.fn();
    fillAndSubmit(submit);
    fireEvent.click(screen.getByRole('radio', { name: /Split-screen Gaming/ }));
    await screen.findByRole('option', { name: 'Loop' });
    fireEvent.change(screen.getByLabelText('Second video (library)'), { target: { value: 'asset-9' } });
    fireEvent.click(screen.getByRole('radio', { name: /Classic/ }));
    fireEvent.click(screen.getByRole('button', { name: /Generate 3 Shorts/ }));
    expect(submit.mock.calls[0][0].options.secondary_asset_id).toBeNull();
  });
});

describe('OutputCard edit summary', () => {
  it('renders timeline_summary and qa_status when present', () => {
    render(<OutputCard onUpdated={vi.fn()} onRegenerate={vi.fn()} short={{
      ...short,
      qa_status: 'flagged',
      timeline_summary: {
        preset: 'gaming_hype', composition: 'blur_fill', caption_renderer: 'ass', removed_seconds: 2.34, punch_ins: 3,
        hook_overlay: { text: 'Wait for it', source: 'transcript' }, reframe_label: 'Motion-based framing',
      },
    }} />);
    const summary = screen.getByLabelText('Edit summary');
    expect(summary).toHaveTextContent('QA flagged');
    expect(summary).toHaveTextContent('gaming hype');
    expect(summary).toHaveTextContent('2.3s');
    expect(summary).toHaveTextContent('Wait for it');
    expect(summary).toHaveTextContent('Motion-based framing');
  });

  it('renders nothing extra and does not break when the keys are absent', () => {
    render(<OutputCard onUpdated={vi.fn()} onRegenerate={vi.fn()} short={{ ...short, timeline_summary: { hook_overlay: null } }} />);
    expect(screen.queryByLabelText('Edit summary')).not.toBeInTheDocument();
    expect(screen.getByText('A real moment')).toBeInTheDocument();
  });
});

describe('ProviderStatus', () => {
  it('shows transcription availability with the backend reason and hosted AI state', async () => {
    api.mockResolvedValue({ asr: false, hosted_inference_enabled: false, reason: 'Install whisper-cli and a multilingual model for offline captions.' });
    render(<ProviderStatus />);
    expect(await screen.findByText('Transcription unavailable')).toBeInTheDocument();
    expect(screen.getByText('Hosted AI off')).toBeInTheDocument();
    expect(screen.getByText(/Install whisper-cli/)).toBeInTheDocument();
  });

  it('reports a failed request instead of guessing', async () => {
    api.mockRejectedValue(new Error('boom'));
    render(<ProviderStatus />);
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Capability status unavailable: boom'));
  });
});
