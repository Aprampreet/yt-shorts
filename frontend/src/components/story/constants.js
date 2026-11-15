export const LANGUAGES = [
  { value: 'en', label: 'English' },
  { value: 'hi', label: 'Hindi' },
  { value: 'hinglish', label: 'Hinglish' },
];

export const CONTENT_FORMATS = [
  { value: 'gaming_news', label: 'Gaming news' },
  { value: 'gameplay_analysis', label: 'Gameplay analysis' },
  { value: 'hidden_details', label: 'Hidden details' },
  { value: 'game_comparison', label: 'Game comparison' },
  { value: 'explainer', label: 'Explainer' },
  { value: 'gaming_commentary', label: 'Gaming commentary' },
  { value: 'myth_vs_fact', label: 'Myth vs fact' },
  { value: 'top_discoveries', label: 'Top discoveries' },
];

export const NARRATION_STYLES = [
  { value: 'energetic', label: 'Energetic' },
  { value: 'natural', label: 'Natural' },
  { value: 'documentary', label: 'Documentary' },
  { value: 'conversational', label: 'Conversational' },
  { value: 'analytical', label: 'Analytical' },
];

export const TARGET_DURATIONS = [
  { value: '20_30', label: '20 to 30 seconds' },
  { value: '30_45', label: '30 to 45 seconds' },
  { value: '45_60', label: '45 to 60 seconds' },
];

export const THEMES = [
  { value: 'gaming_hype', label: 'Gaming Hype' },
  { value: 'cinematic_gaming', label: 'Cinematic Gaming' },
  { value: 'viral_commentary', label: 'Viral Commentary' },
  { value: 'minimal_premium', label: 'Minimal Premium' },
  { value: 'split_screen_gaming', label: 'Split-screen Gaming' },
];

export const CAPTION_PRESETS = [
  { value: 'bold_gaming', label: 'Bold gaming' },
  { value: 'cinematic', label: 'Cinematic' },
  { value: 'dynamic_creator', label: 'Dynamic creator' },
  { value: 'clean_commentary', label: 'Clean commentary' },
];

export const VISUAL_TYPES = [
  { value: 'asset_video', label: 'Library video' },
  { value: 'asset_image', label: 'Library image' },
  { value: 'fact_card', label: 'Fact card' },
  { value: 'comparison_card', label: 'Comparison card' },
  { value: 'callout', label: 'Callout' },
  { value: 'numbered_list', label: 'Numbered list' },
  { value: 'title_card', label: 'Title card' },
  { value: 'source_clip', label: 'Source clip (third party)' },
];

export const TRANSITIONS = [
  { value: 'cut', label: 'Cut' },
  { value: 'dip', label: 'Dip' },
  { value: 'crossfade', label: 'Crossfade' },
];

/** Card kinds and field limits. Mirrors backend/api/editing/card_specs.py. */
export const CARD_FIELDS = {
  fact_card: [
    { key: 'headline', label: 'Headline', max: 90, required: true },
    { key: 'body', label: 'Body', max: 240, required: true, multiline: true },
    { key: 'source_label', label: 'Source label', max: 48 },
  ],
  comparison_card: [
    { key: 'left.label', label: 'Left label', max: 32, required: true },
    { key: 'left.value', label: 'Left value', max: 28, required: true },
    { key: 'right.label', label: 'Right label', max: 32, required: true },
    { key: 'right.value', label: 'Right value', max: 28, required: true },
    { key: 'verdict', label: 'Verdict', max: 80 },
  ],
  callout: [{ key: 'text', label: 'Callout text', max: 90, required: true }],
  numbered_list: [{ key: 'items', label: 'Items', max: 72, required: true, list: true, maxItems: 5 }],
  title_card: [
    { key: 'title', label: 'Title', max: 72, required: true },
    { key: 'kicker', label: 'Kicker', max: 32 },
  ],
};

export const CLAIM_STATUS = {
  source_claim: { label: 'Stated by source', tone: 'neutral' },
  inferred: { label: 'Model inference', tone: 'warn' },
  verified: { label: 'Verified by you', tone: 'success' },
};

export const BILLING_LABELS = { free: 'Free', free_tier: 'Free tier', paid: 'Paid', local: 'Runs locally' };

export const SCRIPT_SECTIONS = [
  { key: 'hook', label: 'Hook', max: 400 },
  { key: 'context', label: 'Context', max: 1200 },
  { key: 'payoff', label: 'Payoff', max: 800 },
  { key: 'cta', label: 'Call to action (optional)', max: 300 },
];

export const COMPONENT_LABELS = {
  structure: 'Structure',
  length_fit: 'Length fit',
  grounding: 'Grounding',
  originality: 'Originality',
  hook: 'Hook',
  readability: 'Readability',
};

export const JOB_LABELS = {
  analyze: 'Analyzing source',
  concepts: 'Generating concepts',
  scripts: 'Writing scripts',
  regenerate: 'Regenerating script',
  render: 'Rendering videos',
};

/** Which story step a job belongs to, from the fields job_data exposes (settings.op, settings.script_ids). */
export function jobKind(job) {
  const settings = job?.settings || {};
  if (settings.op) return settings.op;
  if (settings.script_ids) return 'render';
  return 'analyze';
}

export function formatSeconds(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number.toFixed(1)}s` : '';
}

export function scriptTitle(script, index = 0) {
  return script.title || `Script ${index + 1}`;
}
