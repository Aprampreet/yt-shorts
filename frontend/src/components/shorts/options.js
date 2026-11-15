/** Defaults for the editing options added in the 2.0 API. Sent as-is with every job. */
export const EDITING_DEFAULTS = {
  preset: 'none',
  composition: 'auto',
  caption_preset: 'auto',
  hook_overlay: false,
  remove_dead_air: false,
  punch_ins: false,
  content_category: 'gaming',
  facecam_corner: 'top-right',
  secondary_asset_id: null,
};

export const PRESETS = [
  { value: 'none', label: 'Classic (no preset)', help: 'The original look: your framing and caption choices only, nothing extra applied.' },
  { value: 'gaming_hype', label: 'Gaming Hype', help: 'High-energy pacing with bold gaming captions and emphasis on loud moments.' },
  { value: 'cinematic_gaming', label: 'Cinematic Gaming', help: 'Slower, composed edit with cinematic captions for story and scenery moments.' },
  { value: 'viral_commentary', label: 'Viral Commentary', help: 'Speech-first edit with dynamic captions that keep the spoken words in front.' },
  { value: 'minimal_premium', label: 'Minimal Premium', help: 'Restrained, clean look with understated captions and no heavy effects.' },
  { value: 'split_screen_gaming', label: 'Split-screen Gaming', help: 'Gameplay beside a second video. Needs a second video from your library.' },
];

export const COMPOSITIONS = [
  { value: 'auto', label: 'Auto' },
  { value: 'blur_fill', label: 'Full frame · blurred fill' },
  { value: 'center', label: 'Center crop' },
  { value: 'dynamic_center', label: 'Dynamic center' },
  { value: 'subject_follow', label: 'Subject follow' },
  { value: 'letterbox', label: 'Letterbox' },
  { value: 'split_screen', label: 'Split screen' },
  { value: 'pip', label: 'Picture in picture' },
];

export const CAPTION_PRESETS = [
  { value: 'auto', label: 'Auto' },
  { value: 'bold_gaming', label: 'Bold gaming' },
  { value: 'cinematic', label: 'Cinematic' },
  { value: 'dynamic_creator', label: 'Dynamic creator' },
  { value: 'clean_commentary', label: 'Clean commentary' },
  { value: 'bilingual', label: 'Bilingual' },
];

export const CATEGORIES = [
  { value: 'gaming', label: 'Gaming' },
  { value: 'commentary', label: 'Commentary' },
  { value: 'tutorial', label: 'Tutorial' },
  { value: 'podcast', label: 'Podcast' },
  { value: 'vlog', label: 'Vlog' },
  { value: 'other', label: 'Other' },
];

export const CORNERS = [
  { value: 'top-left', label: 'Top left' },
  { value: 'top-right', label: 'Top right' },
  { value: 'bottom-left', label: 'Bottom left' },
  { value: 'bottom-right', label: 'Bottom right' },
];

export const COMPOSITION_HELP = {
  subject_follow: 'Subject follow uses motion-based framing, not face detection. Check that the action stays in frame.',
  split_screen: 'Split screen places a second video beside your footage.',
  pip: 'Picture in picture keeps a facecam or second video in a corner.',
  center: 'Center crop can cut off action at the edges. Review the result.',
};

export function usesSecondVideo(options) {
  return options.composition === 'split_screen' || options.preset === 'split_screen_gaming';
}

export function usesCorner(options) {
  return usesSecondVideo(options) || options.composition === 'pip';
}
