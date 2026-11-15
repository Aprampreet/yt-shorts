"""Story Studio data contracts: LLM output models, API inputs and scene validation.

LLM output models are strict (extra keys forbidden, bounded lengths) so a provider response either validates or is
rejected; nothing is repaired by guessing. The source text the models are shown is untrusted data.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo, field_validator, model_validator

LANGUAGES = ('en', 'hi', 'hinglish')
CONTENT_FORMATS = ('gaming_news', 'gameplay_analysis', 'hidden_details', 'game_comparison', 'explainer',
                   'gaming_commentary', 'myth_vs_fact', 'top_discoveries')
NARRATION_STYLES = ('energetic', 'natural', 'documentary', 'conversational', 'analytical')
TARGET_DURATIONS = {'20_30': (20, 30), '30_45': (30, 45), '45_60': (45, 60)}
ANGLES = ('explainer', 'timeline', 'myth_vs_fact', 'top_list', 'contrarian', 'comparison', 'deep_dive', 'story')
VISUAL_TYPES = ('asset_video', 'asset_image', 'fact_card', 'comparison_card', 'callout', 'numbered_list',
                'title_card', 'source_clip')
CARD_VISUALS = ('fact_card', 'comparison_card', 'callout', 'numbered_list', 'title_card')
TRANSITIONS = ('cut', 'dip', 'crossfade')
CLAIM_STATUSES = ('source_claim', 'inferred', 'verified')
MAX_TRANSCRIPT_CHARS = 60_000
CLAIM_ID = re.compile(r'^c\d{1,3}$')

Language = Literal['en', 'hi', 'hinglish']
ContentFormat = Literal['gaming_news', 'gameplay_analysis', 'hidden_details', 'game_comparison', 'explainer',
                        'gaming_commentary', 'myth_vs_fact', 'top_discoveries']
NarrationStyle = Literal['energetic', 'natural', 'documentary', 'conversational', 'analytical']
TargetDuration = Literal['20_30', '30_45', '45_60']
Angle = Literal['explainer', 'timeline', 'myth_vs_fact', 'top_list', 'contrarian', 'comparison', 'deep_dive', 'story']


def _text(max_length, min_length=1):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


# ------------------------------------------------------------------------------------- LLM outputs

class ClaimOut(_Strict):
    text: _text(400)
    quote: _text(500, 0) = ''          # verbatim supporting quote copied from the source
    kind: Literal['stated', 'inferred'] = 'stated'


class IntelligenceOut(_Strict):
    summary: _text(800, 0) = ''
    claims: list[ClaimOut] = Field(default_factory=list, max_length=40)
    unknowns: list[_text(240)] = Field(default_factory=list, max_length=12)


class ConceptOut(_Strict):
    angle: Angle
    title: _text(120)
    logline: _text(400)
    hook_idea: _text(240)
    key_points: list[_text(240)] = Field(min_length=1, max_length=6)
    claim_ids: list[_text(8)] = Field(default_factory=list, max_length=12)


class ConceptsOut(_Strict):
    concepts: list[ConceptOut] = Field(min_length=1, max_length=8)


class ScriptSections(_Strict):
    hook: _text(400)
    context: _text(1200)
    core: list[_text(1500)] = Field(min_length=1, max_length=6)
    payoff: _text(800)
    cta: _text(300, 0) = ''


class ScriptOut(_Strict):
    title: _text(120)
    sections: ScriptSections
    quotes: list[_text(400)] = Field(default_factory=list, max_length=3)   # verbatim, attributed source quotes


# ------------------------------------------------------------------------------------- API inputs

class ProjectIn(_Strict):
    url: Optional[str] = Field(None, max_length=2048)
    transcript: Optional[str] = Field(None, max_length=MAX_TRANSCRIPT_CHARS)
    title: str = Field('', max_length=300)
    language: Language = 'en'
    content_format: ContentFormat = 'gaming_commentary'
    narration_style: NarrationStyle = 'natural'
    target_duration: TargetDuration = '30_45'
    video_count: int = Field(3, ge=1, le=3)
    rights_declaration: bool = False
    permission_reference: str = Field('', max_length=2000)
    # url/transcript presence and the rights declaration are checked by the route (clear error codes).


class ProjectUpdateIn(_Strict):
    title: Optional[str] = Field(None, max_length=300)
    music_asset_id: Optional[str] = None       # '' clears it
    theme: Optional[Literal['gaming_hype', 'cinematic_gaming', 'viral_commentary', 'minimal_premium',
                            'split_screen_gaming']] = None
    caption_preset: Optional[Literal['bold_gaming', 'cinematic', 'dynamic_creator', 'clean_commentary']] = None
    video_count: Optional[int] = Field(None, ge=1, le=3)


class ClaimEvidenceIn(_Strict):
    evidence_url: str = Field('', max_length=500)
    evidence_note: str = Field('', max_length=2000)

    @field_validator('evidence_url')
    @classmethod
    def _url(cls, value):
        value = value.strip()
        if value and not re.match(r'^https?://[^\s]{3,}$', value, re.I):
            raise ValueError('evidence_url must be an http(s) URL.')
        return value

    @model_validator(mode='after')
    def _need_one(self):
        self.evidence_note = self.evidence_note.strip()
        if not self.evidence_url and not self.evidence_note:
            raise ValueError('Attach evidence: an evidence_url or an evidence_note.')
        return self


class ConceptSelectIn(_Strict):
    concept_ids: list[str] = Field(min_length=1, max_length=3)


class ScriptsGenerateIn(_Strict):
    force: bool = False   # regenerate concepts that already have a script


class SectionsIn(_Strict):
    hook: _text(400)
    context: _text(1200)
    core: list[_text(1500)] = Field(min_length=1, max_length=6)
    payoff: _text(800)
    cta: _text(300, 0) = ''


class ScriptEditIn(_Strict):
    title: Optional[_text(120)] = None
    sections: Optional[SectionsIn] = None
    full_text: Optional[_text(6000)] = None

    @model_validator(mode='after')
    def _one(self):
        if self.sections is not None and self.full_text is not None:
            raise ValueError('Send sections or full_text, not both.')
        if self.title is None and self.sections is None and self.full_text is None:
            raise ValueError('Nothing to update.')
        return self


class ManualScriptIn(_Strict):
    title: _text(120)
    language: Language = 'en'
    sections: Optional[SectionsIn] = None
    full_text: Optional[_text(6000)] = None

    @model_validator(mode='after')
    def _one(self):
        if (self.sections is None) == (self.full_text is None):
            raise ValueError('Send exactly one of sections or full_text.')
        return self


class NarrateIn(_Strict):
    provider: Optional[str] = Field(None, max_length=40)
    voice_id: str = Field(min_length=1, max_length=120)
    speed: float = 1.0


class AttachNarrationIn(_Strict):
    asset_id: str


class StoryboardIn(_Strict):
    asset_ids: list[str] = Field(default_factory=list, max_length=20)   # preferred library assets
    use_source_clips: bool = False


class RenderIn(_Strict):
    script_ids: list[str] = Field(default_factory=list, max_length=3)  # default: approved scripts of the project


# ------------------------------------------------------------------------------------- scenes

class SceneSpec(_Strict):
    """One storyboard scene. Validate with context={'assets': {id: MediaAsset}, 'project': ..., 'claims': {...},
    'duration': float}. Asset ids must be keys of ``assets`` (the owner's live assets): an id that is not there,
    or a restricted asset, is rejected; nothing is invented."""
    start: float = Field(ge=0)
    end: float
    narration_text: str = Field('', max_length=2000)
    visual_type: Literal['asset_video', 'asset_image', 'fact_card', 'comparison_card', 'callout', 'numbered_list',
                         'title_card', 'source_clip']
    asset_id: Optional[str] = None
    asset_in: Optional[float] = Field(None, ge=0)
    asset_out: Optional[float] = Field(None, ge=0)
    card: Optional[dict] = None          # card_specs payload (kind inferred from visual_type for card scenes)
    claim_id: Optional[str] = None
    transition: Literal['cut', 'dip', 'crossfade'] = 'cut'

    @model_validator(mode='after')
    def _check(self, info: ValidationInfo):
        from ..editing import card_specs
        ctx = info.context or {}
        assets = ctx.get('assets', {})
        if self.end <= self.start:
            raise ValueError('scene end must be after start')
        duration = ctx.get('duration')
        if duration is not None and self.end > duration + 0.05:
            raise ValueError('scene ends after the narration')
        needs_asset = self.visual_type in ('asset_video', 'asset_image', 'source_clip')
        if needs_asset and not self.asset_id:
            raise ValueError(f'{self.visual_type} scenes need an asset_id')
        if not needs_asset and self.asset_id:
            raise ValueError(f'{self.visual_type} scenes are graphics-only; remove asset_id')
        asset = None
        if self.asset_id:
            asset = assets.get(str(self.asset_id))
            if asset is None:
                raise ValueError(f'asset {self.asset_id} is not one of your library assets')
            if asset.license_status == 'restricted':
                raise ValueError(f'asset {self.asset_id} is restricted and cannot be used in a video')
            if self.visual_type == 'asset_video' and asset.kind != 'video':
                raise ValueError('asset_video scenes need a video asset')
            if self.visual_type == 'asset_image' and asset.kind not in ('image', 'graphic'):
                raise ValueError('asset_image scenes need an image asset')
            if self.visual_type == 'asset_video' and asset.origin == 'source_ingest':
                raise ValueError('third-party source footage can only be used as a source_clip scene')
        if self.visual_type == 'source_clip':
            project = ctx.get('project')
            source = getattr(project, 'source', None)
            if asset.origin != 'source_ingest' or source is None or asset.source_video_id != source.pk:
                raise ValueError('source_clip scenes must use this project\'s ingested source video')
            if not source.rights_declaration:
                raise ValueError('source_clip scenes need a rights declaration for the source')
            if not self.claim_id:
                raise ValueError('source_clip scenes must be tied to a specific claim (claim_id); '
                                 'source footage is never used as background filler')
        if self.claim_id:
            claims = ctx.get('claims')
            if claims is not None and self.claim_id not in claims:
                raise ValueError(f'unknown claim {self.claim_id}')
        if asset is not None and asset.kind == 'video':
            a_in = self.asset_in or 0.0
            a_out = self.asset_out if self.asset_out is not None else None
            if a_out is not None and a_out <= a_in:
                raise ValueError('asset_out must be after asset_in')
            if asset.duration and a_in >= asset.duration:
                raise ValueError('asset_in is beyond the asset duration')
        if self.visual_type in CARD_VISUALS:
            if not self.card:
                raise ValueError(f'{self.visual_type} scenes need a card spec')
        if self.card:
            payload = dict(self.card)
            kind = payload.setdefault('kind', self.visual_type if self.visual_type in CARD_VISUALS else 'callout')
            if self.visual_type in CARD_VISUALS and kind != self.visual_type:
                raise ValueError(f'card kind {kind} does not match visual_type {self.visual_type}')
            models = {'fact_card': card_specs.FactCard, 'comparison_card': card_specs.ComparisonCard,
                      'callout': card_specs.Callout, 'numbered_list': card_specs.NumberedList,
                      'title_card': card_specs.TitleCard}
            if kind not in models:
                raise ValueError(f'unsupported card kind {kind}')
            self.card = models[kind].model_validate(payload).model_dump(mode='json')
        return self


class SceneEditIn(_Strict):
    narration_text: Optional[str] = Field(None, max_length=2000)
    visual_type: Optional[Literal['asset_video', 'asset_image', 'fact_card', 'comparison_card', 'callout',
                                  'numbered_list', 'title_card', 'source_clip']] = None
    asset_id: Optional[str] = None       # '' clears the asset
    asset_in: Optional[float] = None
    asset_out: Optional[float] = None
    card: Optional[dict] = None          # {} clears the card
    claim_id: Optional[str] = None       # '' clears
    transition: Optional[Literal['cut', 'dip', 'crossfade']] = None
