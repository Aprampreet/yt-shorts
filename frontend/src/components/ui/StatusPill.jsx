import Badge from './Badge';

const PHASES = {
  queued: ['Queued', 'neutral'],
  downloading: ['Downloading', 'accent'],
  transcribing: ['Transcribing', 'accent'],
  analyzing: ['Analyzing', 'accent'],
  writing: ['Writing', 'accent'],
  generating_audio: ['Generating audio', 'accent'],
  storyboarding: ['Storyboarding', 'accent'],
  rendering: ['Rendering', 'accent'],
  quality_checking: ['Quality check', 'accent'],
  completed: ['Completed', 'success'],
  needs_review: ['Needs review', 'warn'],
  failed: ['Failed', 'danger'],
  cancelled: ['Cancelled', 'neutral'],
};

/** Maps a job `phase` (canonical state from the API) to a labelled pill. Unknown phases are shown as-is. */
export default function StatusPill({ phase }) {
  const [label, tone] = PHASES[phase] || [String(phase || 'Unknown').replace(/_/g, ' '), 'neutral'];
  return <Badge tone={tone}>{label}</Badge>;
}
