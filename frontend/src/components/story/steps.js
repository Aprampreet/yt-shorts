/**
 * Derives the state of every workflow step from GET /story/projects/{id}.
 * Nothing is stored client-side, so a reload resumes exactly where the data says.
 * state: 'complete' | 'available' | 'blocked'
 */
export const STEP_DEFS = [
  { key: 'input', label: 'Source' },
  { key: 'intelligence', label: 'Analyze' },
  { key: 'concepts', label: 'Concepts' },
  { key: 'scripts', label: 'Scripts' },
  { key: 'narration', label: 'Narration' },
  { key: 'storyboard', label: 'Storyboard' },
  { key: 'render', label: 'Render' },
];

export const approvedScripts = project => (project.scripts || []).filter(s => s.status === 'approved');

export function deriveSteps(project) {
  const claims = project.intelligence?.claims || [];
  const analyzed = Boolean(project.intelligence?.method);
  const concepts = project.concepts || [];
  const approved = approvedScripts(project);
  const narrated = approved.filter(s => s.narration);
  const boarded = narrated.filter(s => s.storyboard_ready);
  const make = (state, reason = '') => ({ state, reason });

  return {
    input: make('complete'),
    intelligence: make(analyzed ? 'complete' : 'available'),
    concepts: analyzed && claims.length
      ? make(concepts.some(c => c.selected) ? 'complete' : 'available')
      : make('blocked', analyzed ? 'The source produced no claims to build concepts on' : 'Analyze the source first'),
    scripts: make(approved.length ? 'complete' : 'available'),
    narration: approved.length
      ? make(narrated.length === approved.length ? 'complete' : 'available')
      : make('blocked', 'Approve a script first'),
    storyboard: narrated.length
      ? make(boarded.length === narrated.length ? 'complete' : 'available')
      : make('blocked', approved.length ? 'Add narration to an approved script first' : 'Approve a script first'),
    render: boarded.length
      ? make((project.videos || []).length ? 'complete' : 'available')
      : make('blocked', narrated.length ? 'Build a storyboard first' : 'Approve a script and add narration first'),
  };
}

/** The first step that still needs work, used as the default view on load. */
export function currentStep(steps) {
  const order = STEP_DEFS.map(s => s.key);
  const open = order.find(key => steps[key].state === 'available');
  if (!open) return 'render';
  // Render is the natural next action once everything before it is done.
  return open;
}
