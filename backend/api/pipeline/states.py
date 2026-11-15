"""Canonical job states shared by the Shorts and Story pipelines.

Pure Python (no Django imports) so it can be imported anywhere, including settings-time code.
"""

QUEUED = 'queued'
DOWNLOADING = 'downloading'
TRANSCRIBING = 'transcribing'
ANALYZING = 'analyzing'
WRITING = 'writing'
GENERATING_AUDIO = 'generating_audio'
STORYBOARDING = 'storyboarding'
RENDERING = 'rendering'
QUALITY_CHECKING = 'quality_checking'
COMPLETED = 'completed'
NEEDS_REVIEW = 'needs_review'
FAILED = 'failed'
CANCELLED = 'cancelled'

CANONICAL = (QUEUED, DOWNLOADING, TRANSCRIBING, ANALYZING, WRITING, GENERATING_AUDIO, STORYBOARDING,
             RENDERING, QUALITY_CHECKING, COMPLETED, NEEDS_REVIEW, FAILED, CANCELLED)

# Intermediate states written by the pre-2.0 Shorts pipeline. Still accepted (old rows, running workers).
LEGACY_ALIASES = {
    'preprocessing': ANALYZING,
    'selecting': ANALYZING,
    'editing': RENDERING,
    'captioning': RENDERING,
    'validating': QUALITY_CHECKING,
}

# Must stay equal to api.tasks.TERMINAL (asserted in api/test_wp1_models.py).
TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED, NEEDS_REVIEW})
ACTIVE = frozenset(CANONICAL) - TERMINAL

# Ordered states each pipeline can pass through (excluding terminal states).
PIPELINE_STATES = {
    'shorts': (QUEUED, DOWNLOADING, TRANSCRIBING, ANALYZING, RENDERING, QUALITY_CHECKING),
    'story': (QUEUED, DOWNLOADING, TRANSCRIBING, ANALYZING, WRITING, GENERATING_AUDIO, STORYBOARDING,
              RENDERING, QUALITY_CHECKING),
}

# Story GenerationJob.step -> the state the job reports while running that step.
STORY_STEPS = ('analyze', 'write', 'narrate', 'storyboard', 'render')
STEP_STATES = {'analyze': ANALYZING, 'write': WRITING, 'narrate': GENERATING_AUDIO,
               'storyboard': STORYBOARDING, 'render': RENDERING}

KNOWN = frozenset(CANONICAL) | frozenset(LEGACY_ALIASES)


def phase(status):
    """Map a stored status (canonical or legacy) to its canonical phase.

    Unknown values are returned unchanged so newer states never crash older readers.
    """
    value = (status or '').strip().lower()
    if value in LEGACY_ALIASES:
        return LEGACY_ALIASES[value]
    return value if value in CANONICAL else status


def is_canonical(status):
    return status in CANONICAL


def is_terminal(status):
    return phase(status) in TERMINAL


def is_active(status):
    return not is_terminal(status)
