"""WP12 AI Story Studio: intelligence, concepts, scripts, storyboard, render. Text provider mocked; no network."""
import copy
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase, override_settings

from ..editing import ffmpeg_graph, graphics
from ..editing.ffmpeg_graph import Capabilities
from ..models import GeneratedScript, GeneratedShort, GenerationJob, GenerationProject, MediaAsset, SourceVideo
from ..pipeline import story as story_pipeline
from ..providers import Capability, ProviderUnavailable
from ..services import assets as asset_svc
from ..services.media import probe, run
from ..story import intelligence as intel
from ..story import scoring, scripts as writer
from ..story.schemas import ConceptsOut, IntelligenceOut

CLEAN_ENV = {k: '' for k in ('GROQ_API_KEY', 'GEMINI_API_KEY', 'HF_TOKEN', 'HF_TEXT_MODEL', 'AI_ALLOW_BILLABLE',
                             'AI_ALLOW_PAID_PROVIDERS', 'AI_PROVIDER_ORDER_TEXT', 'ELEVENLABS_API_KEY')}

SOURCE = (
    "Ashfall Ridge patch 2.1 went live on March 14 according to the developer stream. "
    "The patch adds a new boss called the Cinder Warden in the northern caves. "
    "Players reported that the Cinder Warden fight has three separate phases. "
    "In the first phase the boss only uses fire attacks against the player. "
    "The second phase summons two ember hounds that chase you around the arena. "
    "The third phase collapses the arena floor every 40 seconds until the fight ends. "
    "The patch adds a new boss called the Cinder Warden in the northern caves. "
    "The developers said the stamina system was rebalanced for this update. "
    "Sprinting now costs 15 percent less stamina than it did before the patch. "
    "Dodge rolls still cost exactly the same amount of stamina as before. "
    "Merchants in Emberfall town now sell the Ironbark shield for 200 gold coins. "
    "The streamer compared the Ironbark shield with the older Glass Buckler shield. "
    "The Glass Buckler blocks more damage but it breaks after 30 hits in combat. "
    "Gold income from the market quests was also reduced by the shop changes. "
    "A hidden room behind the waterfall contains a torn map fragment. "
    "Several players found that the map fragment points toward the eastern desert. "
    "Nobody has confirmed what is waiting in the eastern desert yet.")

INTEL = {'summary': 'Patch 2.1 notes: a new boss, stamina changes, shield prices and a hidden map fragment.',
         'claims': [
             {'text': 'Patch 2.1 launched on March 14.', 'quote': 'patch 2.1 went live on March 14', 'kind': 'stated'},
             {'text': 'A new boss, the Cinder Warden, is in the northern caves.',
              'quote': 'The patch adds a new boss called the Cinder Warden in the northern caves', 'kind': 'stated'},
             {'text': 'The fight has three phases.', 'quote': 'the Cinder Warden fight has three separate phases', 'kind': 'stated'},
             {'text': 'Phase one is fire attacks only.', 'quote': 'In the first phase the boss only uses fire attacks', 'kind': 'stated'},
             {'text': 'Phase two adds two ember hounds.', 'quote': 'The second phase summons two ember hounds', 'kind': 'stated'},
             {'text': 'Phase three collapses the floor every 40 seconds.',
              'quote': 'The third phase collapses the arena floor every 40 seconds', 'kind': 'stated'},
             {'text': 'Sprinting costs 15 percent less stamina.', 'quote': 'Sprinting now costs 15 percent less stamina', 'kind': 'stated'},
             {'text': 'Ironbark shield costs 200 gold.', 'quote': 'now sell the Ironbark shield for 200 gold coins', 'kind': 'stated'},
             {'text': 'Glass Buckler breaks after 30 hits.', 'quote': 'it breaks after 30 hits in combat', 'kind': 'stated'},
             {'text': 'A map fragment points to the eastern desert.',
              'quote': 'the map fragment points toward the eastern desert', 'kind': 'stated'},
             {'text': 'The desert may hold a future area.', 'quote': '', 'kind': 'inferred'},
             {'text': 'The developer confirmed a desert expansion.', 'quote': 'developers confirmed a desert expansion', 'kind': 'stated'},
         ],
         'unknowns': ['What is in the eastern desert']}


def concept(angle, title, claims, points, hook='Did you notice this?'):
    return {'angle': angle, 'title': title, 'logline': title + ' explained for players.', 'hook_idea': hook,
            'key_points': points, 'claim_ids': claims}


DIVERSE = [
    concept('explainer', 'How the Cinder Warden fight works', ['c2', 'c3', 'c4', 'c5', 'c6'],
            ['phase one fire', 'phase two hounds', 'floor collapse'], 'Why does this boss break the floor?'),
    concept('comparison', 'Ironbark versus Glass Buckler', ['c8', 'c9'], ['price', 'durability', 'verdict'],
            'Which shield should you actually buy?'),
    concept('story', 'The waterfall map mystery', ['c10', 'c11'], ['hidden room', 'map fragment', 'desert'],
            'What is hiding behind the waterfall?'),
]
NEAR_DUPES = [concept('explainer', f'Cinder Warden boss guide part {i}', ['c2', 'c3', 'c4'],
                      ['boss phases guide', 'boss fire attacks guide', 'boss arena guide']) for i in range(6)]

SCRIPT_BOSS = {'title': 'The floor is the real boss', 'quotes': [], 'sections': {
    'hook': 'Three phases and a collapsing floor: is this the meanest fight in Ashfall Ridge?',
    'context': 'According to the video, patch 2.1 dropped a brand-new boss into the northern caves. [c2]',
    'core': ['The creator says round one is pure fire, so stack resistance before you walk in. [c4]',
             'Then, the video claims, a pair of ember hounds joins the party in round two. [c5]',
             'And per the stream, the ground gives way every 40 seconds once you hit the final stage. [c6]'],
    'payoff': 'So the real skill check might be your footwork, not your damage numbers.',
    'cta': 'Tell me which round wrecked you first.'}}
SCRIPT_SHIELD = {'title': 'Cheap shield, real trade-off', 'quotes': [], 'sections': {
    'hook': 'Is the cheaper shield secretly the smarter buy right now?',
    'context': 'According to the video, Emberfall merchants are asking 200 gold for Ironbark. [c8]',
    'core': ['The streamer points out that the Glass Buckler soaks more punishment per block.',
             'But the video says it shatters after 30 hits, which adds up fast in long fights. [c9]',
             'That makes durability, not raw blocking, the stat you should weigh first.'],
    'payoff': 'If you hate repair trips, Ironbark could be the calmer pick for most players.',
    'cta': 'Which one is on your arm tonight?'}}
SCRIPT_MAP = {'title': 'Follow the torn map', 'quotes': [], 'sections': {
    'hook': 'What is waiting past the waterfall, and why is nobody sure yet?',
    'context': 'Players say a secret chamber behind the falls holds a ripped piece of a map. [c10]',
    'core': ['According to the video, that scrap seems to lead toward the eastern desert. [c10]',
             'It might hint at a future zone, but that part is only a guess for now. [c11]',
             'No one in the stream could say what actually waits out there.'],
    'payoff': 'So grab the scrap now, and you could be early when the desert finally opens up.',
    'cta': 'Drop your best theory below.'}}
COPIED = {'title': 'Copied', 'quotes': [], 'sections': {
    'hook': 'The patch adds a new boss called the Cinder Warden in the northern caves.',
    'context': 'Players reported that the Cinder Warden fight has three separate phases.',
    'core': ['In the first phase the boss only uses fire attacks against the player.',
             'The second phase summons two ember hounds that chase you around the arena.',
             'The third phase collapses the arena floor every 40 seconds until the fight ends.'],
    'payoff': 'The developers said the stamina system was rebalanced for this update.', 'cta': ''}}

HINDI = {'hook': 'क्या आपने कभी सोचा है कि एक बॉस फाइट का फ़र्श ही आपका सबसे बड़ा दुश्मन बन सकता है?',
         'context': 'वीडियो के मुताबिक, नए पैच में उत्तर की गुफाओं में एक नया बॉस जोड़ा गया है।',
         'core': ['क्रिएटर का कहना है कि पहले फेज़ में बॉस सिर्फ़ आग वाले हमले करता है।',
                  'दूसरे फेज़ में दो छोटे शिकारी कुत्ते साथ आ जाते हैं, ऐसा वीडियो में बताया गया है।',
                  'आख़िरी फेज़ में ज़मीन बार-बार टूटती है, इसलिए लगातार चलते रहना ज़रूरी है।'],
         'payoff': 'शायद असली परीक्षा आपके हथियार की नहीं, बल्कि आपके क़दमों की है।',
         'cta': 'कमेंट में बताइए, आप किस फेज़ में सबसे ज़्यादा बार हारे?'}
HINGLISH = {'hook': 'Kya aapko pata hai ki is boss fight mein floor hi aapka sabse bada dushman hai?',
            'context': 'Video ke mutabik, naye patch mein northern caves ke andar ek naya boss aaya hai.',
            'core': ['Creator ka kehna hai ki pehle phase mein boss sirf fire attacks use karta hai.',
                     'Second phase mein do ember hounds bhi aa jaate hain, aisa video mein bataya gaya hai.',
                     'Last phase mein zameen baar baar tootti hai, toh rukna matlab seedha haar.'],
            'payoff': 'Shayad asli test aapke damage ka nahi, balki aapki footwork ka hai.',
            'cta': 'Comment mein batao, kaunsa phase sabse tough laga?'}


class FakeText:
    info = SimpleNamespace(name='fake_llm', label='Fake LLM')

    def __init__(self, responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls = []

    def complete_json(self, *, task, system, user, schema, max_tokens):
        self.calls.append({'task': task, 'system': system, 'user': user})
        queue = self.responses[task]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return schema.model_validate(item)


def lavfi(path, spec, seconds=None, extra=()):
    args = ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', spec]
    if seconds:
        args += ['-t', str(seconds)]
    run(args + list(extra) + [str(path)])
    return Path(path)


class StoryBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.override.enable()
        env = patch.dict(os.environ, CLEAN_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.dispatch = patch('api.pipeline.story_tasks.dispatch',
                              side_effect=lambda job: story_pipeline.run_job(str(job.pk)))
        self.dispatch.start()
        self.addCleanup(self.dispatch.stop)
        self.client.get('/api/v1/session')
        self.owner = self.client.session['studio_owner']
        self.fake = None

    def tearDown(self):
        self.override.disable()
        self.tmp.cleanup()

    def use_provider(self, responses=None, unavailable=False):
        self.fake = FakeText(responses or {})

        def resolve(capability, **kwargs):
            if Capability(capability) == Capability.TEXT and not unavailable:
                return self.fake
            error = ProviderUnavailable(f'No {capability} provider is available (Groq: GROQ_API_KEY is not set.).')
            error.reasons = ['Groq: GROQ_API_KEY is not set.', 'Gemini: GEMINI_API_KEY is not set.']
            raise error
        patcher = patch('api.providers.registry.resolve', side_effect=resolve)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.fake

    def post(self, url, data=None, client=None):
        return (client or self.client).post(url, data or {}, content_type='application/json')

    def put(self, url, data, client=None):
        return (client or self.client).put(url, data, content_type='application/json')

    def project(self, **kw):
        body = dict(transcript=SOURCE, language='en', content_format='hidden_details', target_duration='30_45',
                    video_count=3, title='Patch 2.1')
        body.update(kw)
        response = self.post('/api/v1/story/projects', body)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()

    def asset(self, name, kind, license_status='owned', origin='upload', owner=None, **meta):
        folder = asset_svc.owner_dir(owner or self.owner)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder/name
        if kind == 'narration':
            lavfi(path, f'sine=frequency={meta.pop("freq", 440)}:duration={meta.pop("seconds", 6)}', extra=['-ac', '1'])
        elif kind == 'image':
            colour = '0x%06x' % (uuid.uuid4().int & 0xFFFFFF)
            lavfi(path, f'testsrc2=size=640x360,drawbox=x=0:y=0:w=200:h=200:color={colour}:t=fill', extra=['-frames:v', '1'])
        elif kind == 'video':
            lavfi(path, 'testsrc2=size=640x360:rate=30', seconds=meta.pop('seconds', 4), extra=['-pix_fmt', 'yuv420p'])
        asset = asset_svc.register_generated_asset(owner or self.owner, path, kind, origin, 'owned' if license_status == 'attribution_required' else license_status,
                                                   title=meta.pop('title', name))
        if license_status == 'attribution_required':
            MediaAsset.objects.filter(pk=asset.pk).update(license_status=license_status, attribution_text='by Tester')
            asset.refresh_from_db()
        return asset

    def manual(self, project, sections=None, title='Manual', language='en'):
        body = {'title': title, 'sections': sections or SCRIPT_BOSS['sections'], 'language': language}
        return self.post(f'/api/v1/story/projects/{project["id"]}/scripts/manual', body).json()

    def narrated(self, project, sections=None, seconds=8, freq=440, language='en'):
        script = self.manual(project, sections, language=language)
        narration = self.asset(f'n{freq}.wav', 'narration', origin='manual_narration', freq=freq, seconds=seconds)
        response = self.post(f'/api/v1/story/scripts/{script["id"]}/narration', {'asset_id': str(narration.pk)})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def analyzed(self, **kw):
        self.use_provider({'story_intelligence': [INTEL],
                           'story_concepts': [{'concepts': DIVERSE + NEAR_DUPES[:3]}],
                           'story_script': [SCRIPT_BOSS, SCRIPT_SHIELD, SCRIPT_MAP]})
        project = self.project(**kw)
        job = self.post(f'/api/v1/story/projects/{project["id"]}/analyze').json()
        self.assertEqual(GenerationJob.objects.get(pk=job['id']).status, 'completed')
        return project


# ------------------------------------------------------------------------------------- pure functions

class IntelligenceUnitTests(SimpleTestCase):
    def test_sectioning_and_dedupe(self):
        result = intel.build_intelligence(SOURCE)
        self.assertEqual(result['source']['duplicates_removed'], 1)
        self.assertIn('Cinder Warden', result['duplicates'][0]['text'])
        self.assertGreaterEqual(len(result['sections']), 2)
        covered = sum(s['units'] for s in result['sections'])
        self.assertEqual(covered, result['source']['kept_units'])
        self.assertEqual(result['method'], 'deterministic')
        self.assertTrue(result['text_provider_unavailable'])
        self.assertTrue(all(c['status'] == 'source_claim' and c['quote'] for c in result['claims']))
        self.assertTrue(any('15 percent' in c['text'] for c in result['claims']))

    def test_quote_verification(self):
        units, _ = intel.dedupe([{'text': t} for t in intel.split_units(SOURCE)])
        sections = intel.build_sections(units)
        claims = intel.verify_model_claims(IntelligenceOut.model_validate(INTEL), units, sections)
        by_text = {c['text']: c for c in claims}
        self.assertEqual(by_text['Phase two adds two ember hounds.']['status'], 'source_claim')
        self.assertTrue(by_text['Phase two adds two ember hounds.']['quote_verified'])
        fabricated = by_text['The developer confirmed a desert expansion.']
        self.assertEqual(fabricated['status'], 'inferred')
        self.assertIn('downgraded', fabricated['note'])
        self.assertEqual(by_text['The desert may hold a future area.']['status'], 'inferred')
        self.assertFalse(any(c['status'] == 'verified' for c in claims))

    def test_unicode_hindi_units(self):
        units = intel.split_units('पहला वाक्य यहाँ है। दूसरा वाक्य 2024 में आया।')
        self.assertEqual(len(units), 2)
        self.assertTrue(intel.is_claim_like(units[1]))


class ScoringUnitTests(SimpleTestCase):
    def claims(self):
        units, _ = intel.dedupe([{'text': t} for t in intel.split_units(SOURCE)])
        return {c['id']: c for c in intel.verify_model_claims(IntelligenceOut.model_validate(INTEL), units,
                                                              intel.build_sections(units))}

    def test_originality_rejects_copy_accepts_rewording(self):
        copied = scoring.originality(scoring.full_text(COPIED['sections']), SOURCE)
        self.assertFalse(copied['passed'])
        self.assertGreater(copied['containment_5'], 0.15)
        self.assertGreater(copied['longest_run'], 12)
        reworded = scoring.originality(scoring.full_text(SCRIPT_BOSS['sections']), SOURCE)
        self.assertTrue(reworded['passed'], reworded)
        report = scoring.score_script(COPIED['sections'], language='en', target_duration='30_45',
                                      claims=self.claims(), source_text=SOURCE)
        self.assertFalse(report['passed'])
        self.assertFalse(report['gates']['originality'])

    def test_good_english_script_passes_and_grounding_rules(self):
        claims = self.claims()
        report = scoring.score_script(SCRIPT_BOSS['sections'], language='en', target_duration='30_45',
                                      claims=claims, source_text=SOURCE)
        self.assertTrue(report['passed'], report['reasons'])
        self.assertEqual(report['wpm'], 150)
        bad = copy.deepcopy(SCRIPT_BOSS['sections'])
        bad['core'][0] = 'Round one is pure fire attacks. [c4]'                      # source claim without attribution
        bad['core'][1] = 'The desert is a future area. [c11]'                       # inference stated as fact
        bad['payoff'] = 'The boss has 900 health and arrives in 2027.'              # invented numbers
        bad_report = scoring.score_script(bad, language='en', target_duration='30_45', claims=claims, source_text=SOURCE)
        joined = ' | '.join(bad_report['reasons'])
        self.assertIn('c4 is a source claim', joined)
        self.assertIn('c11 is an inference', joined)
        self.assertEqual(set(bad_report['unsupported_numbers']), {'900', '2027'})
        self.assertLess(bad_report['components']['grounding'], 0.5)
        unknown = copy.deepcopy(SCRIPT_BOSS['sections'])
        unknown['context'] += ' [c99]'
        self.assertIn('cites unknown claim c99', scoring.score_script(unknown, language='en', target_duration='30_45',
                                                                       claims=claims)['reasons'])

    def test_language_fixtures_length_and_structure(self):
        for sections, lang, wpm in ((SCRIPT_BOSS['sections'], 'en', 150), (HINDI, 'hi', 130), (HINGLISH, 'hinglish', 140)):
            report = scoring.score_script(sections, language=lang, target_duration='30_45')
            self.assertEqual(report['wpm'], wpm)
            self.assertEqual(report['components']['structure'], 1.0, (lang, report['reasons']))
            self.assertEqual(report['components']['length_fit'], 1.0, (lang, report['explanations']))
            self.assertAlmostEqual(report['estimated_duration'], round(report['word_count'] / wpm * 60, 1))
            self.assertTrue(report['passed'], (lang, report['reasons']))
        short = scoring.score_script(HINDI, language='hi', target_duration='20_30')
        self.assertLess(short['components']['length_fit'], 1.0)

    def test_duplicate_guard(self):
        text = scoring.full_text(SCRIPT_BOSS['sections'])
        self.assertEqual(len(scoring.near_duplicates([text, text + ' Wow.', scoring.full_text(SCRIPT_MAP['sections'])])), 1)

    def test_diversity_selection(self):
        pool = [dict(c, quality_score=0.9) for c in NEAR_DUPES[:3]] + [dict(c, quality_score=0.6) for c in DIVERSE]
        picked, min_d = scoring.select_diverse(pool, 3)
        self.assertTrue({4, 5} <= set(picked), picked)   # shield + map concepts, plus one boss concept
        self.assertGreaterEqual(min_d, 0.35)
        dupes, dupe_d = scoring.select_diverse([dict(c, quality_score=0.9) for c in NEAR_DUPES], 3)
        self.assertLess(dupe_d, 0.35)


# ------------------------------------------------------------------------------------- API + pipeline

class ProjectApiTests(StoryBase):
    def test_creation_validation(self):
        self.assertEqual(self.post('/api/v1/story/projects', {'language': 'en'}).json()['error'], 'input_required')
        response = self.post('/api/v1/story/projects', {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'})
        self.assertEqual(response.json()['error'], 'rights_required')
        bad = self.post('/api/v1/story/projects', {'url': 'https://example.com/v', 'rights_declaration': True})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.post('/api/v1/story/projects', {'transcript': 'x' * 60001}).status_code, 422)
        self.assertEqual(self.post('/api/v1/story/projects', {'transcript': 'hi', 'language': 'fr'}).status_code, 422)
        text = 'नया बॉस आया है 🔥 and the Cinder Warden is here.'
        created = self.project(transcript=text)
        self.assertEqual(GenerationProject.objects.get(pk=created['id']).source_notes, text)
        with_url = self.project(url='https://www.youtube.com/watch?v=dQw4w9WgXcQ', rights_declaration=True, transcript='')
        self.assertTrue(with_url['source']['rights_declaration'])
        listing = self.client.get('/api/v1/story/projects').json()['projects']
        self.assertEqual(len(listing), 2)

    def test_owner_isolation_and_csrf(self):
        project = self.project()
        stranger = Client()
        stranger.get('/api/v1/session')
        self.assertEqual(stranger.get(f'/api/v1/story/projects/{project["id"]}').status_code, 404)
        self.assertEqual(self.post(f'/api/v1/story/projects/{project["id"]}/analyze', client=stranger).status_code, 404)
        self.assertEqual(stranger.delete(f'/api/v1/story/projects/{project["id"]}').status_code, 404)
        self.assertEqual(stranger.get('/api/v1/story/projects').json()['projects'], [])
        script = self.post(f'/api/v1/story/projects/{project["id"]}/scripts/manual',
                           {'title': 'Mine', 'sections': SCRIPT_BOSS['sections']}).json()
        self.assertEqual(self.put(f'/api/v1/story/scripts/{script["id"]}', {'title': 'x'}, client=stranger).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        token = csrf.get('/api/v1/session').json()['csrf_token']
        self.assertEqual(csrf.post('/api/v1/story/projects', {'transcript': 'abc def'}, content_type='application/json').status_code, 403)
        ok = csrf.post('/api/v1/story/projects', {'transcript': 'abc def'}, content_type='application/json',
                       HTTP_X_CSRFTOKEN=token)
        self.assertEqual(ok.status_code, 201)

    def test_no_text_provider_and_manual_path(self):
        self.use_provider(unavailable=True)
        project = self.project()
        job = self.post(f'/api/v1/story/projects/{project["id"]}/analyze')
        self.assertEqual(job.status_code, 202)
        detail = self.client.get(f'/api/v1/story/projects/{project["id"]}').json()
        self.assertEqual(detail['intelligence']['method'], 'deterministic')
        self.assertTrue(detail['intelligence']['text_provider_unavailable'])
        self.assertIn('No text provider', detail['intelligence']['notes'][0])
        self.assertNotIn('source_text', detail['intelligence'])
        self.assertFalse(detail['text_provider']['available'])
        response = self.post(f'/api/v1/story/projects/{project["id"]}/concepts/generate')
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body['error'], 'text_provider_unavailable')
        self.assertTrue(body['reasons'])
        self.assertFalse(GenerationJob.objects.filter(project_id=project['id'], step='write').exists())
        manual = self.post(f'/api/v1/story/projects/{project["id"]}/scripts/manual',
                           {'title': 'My take', 'full_text': scoring.full_text(SCRIPT_BOSS['sections'])})
        self.assertEqual(manual.status_code, 201, manual.content)
        data = manual.json()
        self.assertTrue(data['manual'])
        self.assertIsNone(data['concept_id'])
        self.assertEqual(data['title'], 'My take')
        self.assertTrue(data['sections']['hook'])
        self.assertIn('score', data['quality_report'])
        regen = self.post(f'/api/v1/story/scripts/{data["id"]}/regenerate')
        self.assertEqual(regen.json()['error'], 'manual_script')

    def test_claim_evidence_marks_verified(self):
        project = self.analyzed()
        detail = self.client.get(f'/api/v1/story/projects/{project["id"]}').json()
        claims = detail['intelligence']['claims']
        self.assertEqual(detail['intelligence']['method'], 'llm')
        self.assertEqual(claims[0]['status'], 'source_claim')
        self.assertEqual(claims[-1]['status'], 'inferred')   # fabricated quote downgraded
        url = f'/api/v1/story/projects/{project["id"]}/claims/c1'
        self.assertEqual(self.put(url, {}).status_code, 422)
        self.assertEqual(self.put(url, {'evidence_url': 'not a url'}).status_code, 422)
        claim = self.put(url, {'evidence_url': 'https://example.com/patch-notes'}).json()
        self.assertEqual(claim['status'], 'verified')
        self.assertEqual(claim['previous_status'], 'source_claim')
        self.assertEqual(self.put(f'/api/v1/story/projects/{project["id"]}/claims/c999', {'evidence_note': 'x'}).status_code, 404)
        reverted = self.client.delete(url + '/evidence').json()
        self.assertEqual(reverted['status'], 'source_claim')

    def test_url_analysis_reuses_ingest_and_transcription(self):
        self.use_provider({'story_intelligence': [INTEL]})
        project = self.project(url='https://www.youtube.com/watch?v=dQw4w9WgXcQ', rights_declaration=True, transcript='')
        sample = lavfi(Path(self.tmp.name)/'src.mp4', 'testsrc2=size=320x180:rate=25', 6,
                       ['-f', 'lavfi', '-i', 'sine=frequency=300:duration=6', '-shortest', '-pix_fmt', 'yuv420p'])

        def fake_download(url, folder):
            target = folder/'source.mp4'
            shutil.copyfile(sample, target)
            return target, {'title': 'Patch talk', 'uploader': 'Someone'}
        cues = [{'start': i * 0.5, 'end': i * 0.5 + 0.45, 'text': t} for i, t in enumerate(intel.split_units(SOURCE)[:10])]
        with patch('api.pipeline.ingest.download', side_effect=fake_download), \
                patch('api.pipeline.transcribe.transcribe_source', return_value=cues):
            job = self.post(f'/api/v1/story/projects/{project["id"]}/analyze').json()
        job_row = GenerationJob.objects.get(pk=job['id'])
        self.assertEqual(job_row.status, 'completed', job_row.error_message)
        self.assertEqual(job_row.pipeline, 'story')
        self.assertIn('intelligence', job_row.analysis['timings'])
        detail = self.client.get(f'/api/v1/story/projects/{project["id"]}').json()
        self.assertEqual(detail['intelligence']['transcript']['cues'], 10)
        stamped = [c for c in detail['intelligence']['claims'] if c['status'] == 'source_claim']
        self.assertTrue(stamped and all('start' in c for c in stamped))
        source_asset = MediaAsset.objects.get(owner=self.owner, origin='source_ingest')
        self.assertEqual(source_asset.license_status, 'permission_pending')
        self.assertEqual(str(source_asset.source_video_id), str(GenerationProject.objects.get(pk=project['id']).source_id))


class ConceptAndScriptTests(StoryBase):
    def test_concepts_pick_diverse_trio(self):
        project = self.analyzed()
        job = self.post(f'/api/v1/story/projects/{project["id"]}/concepts/generate').json()
        self.assertEqual(GenerationJob.objects.get(pk=job['id']).status, 'completed')
        detail = self.client.get(f'/api/v1/story/projects/{project["id"]}').json()
        selected = [c for c in detail['concepts'] if c['selected']]
        self.assertEqual({c['angle'] for c in selected}, {'explainer', 'comparison', 'story'})
        self.assertEqual(len(detail['concepts']), 6)
        self.assertFalse(detail['intelligence']['concept_selection']['regenerated'])
        self.assertEqual(sum(1 for c in self.fake.calls if c['task'] == 'story_concepts'), 1)
        self.assertTrue(all(c['diversity_score'] >= 0.35 for c in selected))
        self.assertIn('untrusted', self.fake.calls[-1]['system'])

    def test_near_duplicate_concepts_trigger_one_regeneration(self):
        self.use_provider({'story_intelligence': [INTEL],
                           'story_concepts': [{'concepts': NEAR_DUPES}, {'concepts': DIVERSE}]})
        project = self.project()
        self.post(f'/api/v1/story/projects/{project["id"]}/analyze')
        self.post(f'/api/v1/story/projects/{project["id"]}/concepts/generate')
        concept_calls = [c for c in self.fake.calls if c['task'] == 'story_concepts']
        self.assertEqual(len(concept_calls), 2)
        self.assertIn('already covered', concept_calls[1]['user'])
        detail = self.client.get(f'/api/v1/story/projects/{project["id"]}').json()
        self.assertTrue(detail['intelligence']['concept_selection']['regenerated'])
        selected = [c for c in detail['concepts'] if c['selected']]
        self.assertEqual(len({c['angle'] for c in selected}), 3)

    def test_scripts_generate_select_and_approve(self):
        project = self.analyzed()
        self.post(f'/api/v1/story/projects/{project["id"]}/concepts/generate')
        concepts = self.client.get(f'/api/v1/story/projects/{project["id"]}').json()['concepts']
        two = [c['id'] for c in concepts if c['selected']][:2]
        self.assertEqual(self.post(f'/api/v1/story/projects/{project["id"]}/concepts/select',
                                   {'concept_ids': [str(uuid.uuid4())]}).json()['error'], 'invalid_concepts')
        self.post(f'/api/v1/story/projects/{project["id"]}/concepts/select', {'concept_ids': two})
        job = self.post(f'/api/v1/story/projects/{project["id"]}/scripts/generate').json()
        self.assertEqual(GenerationJob.objects.get(pk=job['id']).status, 'completed')
        scripts = self.client.get(f'/api/v1/story/projects/{project["id"]}').json()['scripts']
        self.assertEqual(len(scripts), 2)
        self.assertTrue(all(s['quality_report']['passed'] and s['status'] == 'draft' for s in scripts))
        system = [c for c in self.fake.calls if c['task'] == 'story_script'][0]['system']
        for rule in ('untrusted data', 'Never invent', '[c3]', 'Tone example'):
            self.assertIn(rule, system)
        approved = self.post(f'/api/v1/story/scripts/{scripts[0]["id"]}/approve').json()
        self.assertEqual(approved['status'], 'approved')
        edited = self.put(f'/api/v1/story/scripts/{scripts[0]["id"]}',
                          {'sections': dict(SCRIPT_BOSS['sections'], payoff='So footwork might matter most here.')}).json()
        self.assertEqual(edited['status'], 'draft')
        self.assertTrue(edited['quality_report']['edited_by_user'])

    def test_regeneration_bounded_best_kept_needs_review(self):
        weak = copy.deepcopy(SCRIPT_BOSS)
        weak['sections']['core'] = ['Fire. [c4]']
        weak['sections']['context'] = 'New boss.'
        weaker = copy.deepcopy(COPIED)
        self.use_provider({'story_intelligence': [INTEL], 'story_concepts': [{'concepts': DIVERSE}],
                           'story_script': [weaker, weak, weaker, weaker]})
        project = self.project(video_count=1)
        self.post(f'/api/v1/story/projects/{project["id"]}/analyze')
        self.post(f'/api/v1/story/projects/{project["id"]}/concepts/generate')
        job = self.post(f'/api/v1/story/projects/{project["id"]}/scripts/generate').json()
        self.assertEqual(GenerationJob.objects.get(pk=job['id']).status, 'needs_review')
        self.assertEqual(sum(1 for c in self.fake.calls if c['task'] == 'story_script'), 3)
        script = GeneratedScript.objects.get(project_id=project['id'])
        self.assertEqual(script.attempts, 3)
        self.assertEqual(script.status, 'needs_review')
        self.assertEqual(script.sections['context'], 'New boss.')     # best attempt kept
        self.assertTrue(script.quality_report['reasons'])
        self.assertIn('Fix these problems', self.fake.calls[-1]['user'])
        self.assertEqual(self.post(f'/api/v1/story/scripts/{script.pk}/approve').json()['error'], 'quality_below_threshold')
        regen = self.post(f'/api/v1/story/scripts/{script.pk}/regenerate').json()
        self.assertEqual(regen['attempts_left'], 3)
        script.refresh_from_db()
        self.assertEqual(script.attempts, 6)
        self.assertEqual(self.post(f'/api/v1/story/scripts/{script.pk}/regenerate').json()['error'], 'regeneration_limit')


class StoryboardTests(StoryBase):
    def test_requires_narration_and_falls_back_to_cards(self):
        self.use_provider(unavailable=True)
        project = self.project()
        script = self.manual(project)
        self.assertEqual(self.post(f'/api/v1/story/scripts/{script["id"]}/storyboard').json()['error'], 'narration_required')
        script = self.narrated(project)
        board = self.post(f'/api/v1/story/scripts/{script["id"]}/storyboard').json()
        self.assertTrue(board['report']['graphics_only'])
        scenes = board['scenes']
        self.assertTrue(all(s['visual_type'] in ('title_card', 'fact_card', 'callout', 'numbered_list') for s in scenes))
        self.assertEqual(scenes[0]['start'], 0)
        self.assertAlmostEqual(scenes[-1]['end'], 8, delta=0.05)
        self.assertTrue(all(a['end'] == b['start'] for a, b in zip(scenes, scenes[1:])))
        self.assertTrue(all(s['card'] and s['license_badge']['status'] == 'generated' for s in scenes))

    def test_rejects_unknown_foreign_and_restricted_assets(self):
        self.use_provider(unavailable=True)
        project = self.project()
        script = self.narrated(project)
        scene = self.post(f'/api/v1/story/scripts/{script["id"]}/storyboard').json()['scenes'][1]
        url = f'/api/v1/story/scenes/{scene["id"]}'
        unknown = self.put(url, {'visual_type': 'asset_image', 'asset_id': str(uuid.uuid4())}).json()
        self.assertEqual(unknown['error'], 'invalid_scene')
        self.assertIn('not one of your library assets', unknown['message'])
        foreign = self.asset('theirs.png', 'image', owner='someone-else')
        self.assertEqual(self.put(url, {'visual_type': 'asset_image', 'asset_id': str(foreign.pk)}).json()['error'], 'invalid_scene')
        restricted = self.asset('locked.png', 'image', license_status='restricted')
        res = self.put(url, {'visual_type': 'asset_image', 'asset_id': str(restricted.pk)}).json()
        self.assertIn('restricted', res['message'])
        board = self.post(f'/api/v1/story/scripts/{script["id"]}/storyboard', {'asset_ids': [str(restricted.pk)]}).json()
        self.assertEqual(board['error'], 'invalid_scene')
        owned = self.asset('mine.png', 'image')
        ok = self.put(url, {'visual_type': 'asset_image', 'asset_id': str(owned.pk)})
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(ok.json()['asset']['license_status'], 'owned')
        self.assertIsNone(ok.json()['card'])
        card = self.put(url, {'visual_type': 'comparison_card', 'card': {'left': {'label': 'Ironbark', 'value': '200 gold'},
                                                                         'right': {'label': 'Buckler', 'value': '30 hits'}}})
        self.assertEqual(card.status_code, 200, card.content)
        self.assertIsNone(card.json()['asset'])
        bad_card = self.put(url, {'visual_type': 'callout', 'card': {'text': ''}})
        self.assertEqual(bad_card.json()['error'], 'invalid_scene')

    def test_source_clip_rules_and_share_cap(self):
        self.use_provider(unavailable=True)
        project = self.project()
        self.post(f'/api/v1/story/projects/{project["id"]}/analyze')
        script = self.narrated(project, seconds=10)
        clip = self.asset('src.mp4', 'video', license_status='permission_pending', origin='source_ingest', seconds=10)
        scenes = self.post(f'/api/v1/story/scripts/{script["id"]}/storyboard').json()['scenes']
        url = lambda s: f'/api/v1/story/scenes/{s["id"]}'
        no_source = self.put(url(scenes[1]), {'visual_type': 'source_clip', 'asset_id': str(clip.pk), 'claim_id': 'c1'}).json()
        self.assertIn("project's ingested source", no_source['message'])
        source = SourceVideo.objects.create(source_url='https://www.youtube.com/watch?v=x', rights_declaration=True)
        GenerationProject.objects.filter(pk=project['id']).update(source=source)
        MediaAsset.objects.filter(pk=clip.pk).update(source_video=source)
        no_claim = self.put(url(scenes[1]), {'visual_type': 'source_clip', 'asset_id': str(clip.pk), 'claim_id': ''}).json()
        self.assertIn('claim', no_claim['message'])
        as_filler = self.put(url(scenes[1]), {'visual_type': 'asset_video', 'asset_id': str(clip.pk)}).json()
        self.assertIn('source_clip', as_filler['message'])
        claim_id = GenerationProject.objects.get(pk=project['id']).intelligence['claims'][0]['id']
        total, applied, errors = 10.0, 0.0, []
        for scene in sorted(scenes[1:], key=lambda s: s['end'] - s['start']):
            response = self.put(url(scene), {'visual_type': 'source_clip', 'asset_id': str(clip.pk), 'claim_id': claim_id,
                                             'asset_in': 0})
            if response.status_code == 200:
                applied += scene['end'] - scene['start']
            else:
                errors.append(response.json()['error'])
        self.assertIn('source_clip_share', errors)
        self.assertLessEqual(applied / total, 0.35 + 1e-6)
        share = self.client.get(f'/api/v1/story/scripts/{script["id"]}/scenes').json()['source_clip_share']
        self.assertLessEqual(share, 0.35 + 1e-3)


class RenderTests(StoryBase):
    def test_hindi_without_shaping_fails_clearly(self):
        self.use_provider(unavailable=True)
        project = self.project(language='hi', transcript='नया बॉस आया है। उसके तीन फेज़ हैं।')
        script = self.narrated(project, HINDI, language='hi')
        self.post(f'/api/v1/story/scripts/{script["id"]}/storyboard')
        self.post(f'/api/v1/story/scripts/{script["id"]}/approve?override_quality=true')
        caps = Capabilities('test', frozenset({'overlay', 'zoompan', 'xfade'}), False)
        with patch.object(graphics, 'raqm_available', return_value=False), \
                patch('api.editing.story_compiler.ffmpeg_capabilities', return_value=caps):
            job = self.post(f'/api/v1/story/projects/{project["id"]}/render').json()
        row = GenerationJob.objects.get(pk=job['id'])
        self.assertEqual(row.status, 'failed')
        self.assertEqual(row.error_code, 'text_shaping_unavailable')
        self.assertIn('Devanagari', row.error_message)
        self.assertFalse(GeneratedShort.objects.filter(job=row).exists())

    def test_duplicate_scripts_and_restricted_refused(self):
        self.use_provider(unavailable=True)
        project = self.project()
        a = self.narrated(project, freq=300)
        b = self.narrated(project, freq=500)
        for s in (a, b):
            self.post(f'/api/v1/story/scripts/{s["id"]}/storyboard')
            self.post(f'/api/v1/story/scripts/{s["id"]}/approve?override_quality=true')
        response = self.post(f'/api/v1/story/projects/{project["id"]}/render')
        self.assertEqual(response.json()['error'], 'duplicate_scripts')
        self.assertFalse(GenerationJob.objects.filter(project_id=project['id'], step='render').exists())
        locked = self.asset('locked.png', 'image')
        scene = GeneratedScript.objects.get(pk=a['id']).scenes.order_by('order')[1]
        self.put(f'/api/v1/story/scenes/{scene.pk}', {'visual_type': 'asset_image', 'asset_id': str(locked.pk)})
        MediaAsset.objects.filter(pk=locked.pk).update(license_status='restricted')
        refused = self.post(f'/api/v1/story/projects/{project["id"]}/render', {'script_ids': [a['id']]}).json()
        self.assertEqual(refused['error'], 'restricted_asset')

    def test_end_to_end_three_story_videos(self):
        project = self.analyzed()
        pid = project['id']
        self.post(f'/api/v1/story/projects/{pid}/concepts/generate')
        job = self.post(f'/api/v1/story/projects/{pid}/scripts/generate').json()
        self.assertEqual(GenerationJob.objects.get(pk=job['id']).status, 'completed')
        scripts = self.client.get(f'/api/v1/story/projects/{pid}').json()['scripts']
        self.assertEqual(len(scripts), 3)
        image = self.asset('art.png', 'image', title='Cinder Warden boss artwork')
        video = self.asset('clip.mp4', 'video', title='My gameplay', seconds=3)
        for i, script in enumerate(scripts):
            self.assertEqual(self.post(f'/api/v1/story/scripts/{script["id"]}/approve').json()['status'], 'approved')
            narration = self.asset(f'n{i}.wav', 'narration', origin='manual_narration', freq=300 + 150 * i, seconds=6 + i)
            self.post(f'/api/v1/story/scripts/{script["id"]}/narration', {'asset_id': str(narration.pk)})
            body = {'asset_ids': [str(image.pk), str(video.pk)]} if i == 0 else {}
            board = self.post(f'/api/v1/story/scripts/{script["id"]}/storyboard', body).json()
            self.assertIn('scenes', board, board)
            if i == 0:
                types = [s['visual_type'] for s in board['scenes']]
                self.assertIn('asset_image', types)
                self.assertIn('asset_video', types)
        started = time.monotonic()
        response = self.post(f'/api/v1/story/projects/{pid}/render')
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 202, response.content)
        row = GenerationJob.objects.get(pk=response.json()['id'])
        self.assertIn(row.status, ('completed', 'needs_review'), (row.error_code, row.error_message, row.analysis))
        self.assertEqual(row.analysis['failures'], [])
        print(f'\n[story e2e] rendered 3 story videos in {elapsed:.1f}s')
        videos = list(GeneratedShort.objects.filter(job=row).order_by('short_number'))
        self.assertEqual([v.short_number for v in videos], [1, 2, 3])
        hashes, transcripts = set(), set()
        for i, video_row in enumerate(videos):
            self.assertEqual(video_row.kind, 'story')
            self.assertEqual(str(video_row.project_id), pid)
            self.assertIsNotNone(video_row.script_id)
            path = Path(self.tmp.name)/video_row.video_file
            info = probe(path)
            self.assertEqual((info['width'], info['height']), (720, 1280))
            self.assertTrue(info['has_audio'])
            self.assertAlmostEqual(info['duration'], 6 + i, delta=0.4)
            report = video_row.quality_report
            self.assertTrue(all(report['qa']['checks'].values()), report['qa'])
            self.assertIn(video_row.qa_status, ('passed', 'flagged'))
            self.assertEqual(video_row.rights_review_status, 'rights_documented')
            self.assertFalse(report['narration']['synthetic_voice'])
            self.assertIn('passed', report['script_quality'])
            self.assertTrue(any(p['used_as'] == 'narration' for p in report['provenance']))
            self.assertEqual(video_row.transcript, GeneratedScript.objects.get(pk=video_row.script_id).full_text)
            segments = list(video_row.segments.all())
            self.assertTrue(segments)
            self.assertTrue(all(s.end_ms > s.start_ms for s in segments))
            self.assertLessEqual(segments[-1].end_ms, round(info['duration'] * 1000) + 50)
            self.assertEqual(' '.join(s.original_text for s in segments).split(), video_row.transcript.split())
            self.assertTrue((Path(self.tmp.name)/video_row.thumbnail_file).is_file())
            self.assertTrue(video_row.timeline['overlays'])
            self.assertIn('not independently fact-checked', video_row.description)
            hashes.add(asset_svc.sha256_of(path))
            transcripts.add(video_row.transcript)
        first = videos[0].quality_report['provenance']
        used = {p['asset_id']: p for p in first}
        self.assertEqual(used[str(image.pk)]['license_status'], 'owned')
        self.assertEqual(used[str(video.pk)]['used_as'], 'scene_video')
        self.assertEqual(len(hashes), 3)
        self.assertEqual(len(transcripts), 3)
        # served by the existing Shorts routes
        self.assertEqual(self.client.get(f'/api/v1/shorts/{videos[0].pk}/video').status_code, 200)
        self.assertEqual(self.client.get(f'/api/v1/shorts/{videos[0].pk}/captions').status_code, 200)
        detail = self.client.get(f'/api/v1/story/projects/{pid}').json()
        self.assertEqual(len(detail['videos']), 3)
        self.assertEqual(detail['videos'][0]['kind'], 'story')
        polled = self.client.get(f'/api/v1/jobs/{row.pk}').json()
        self.assertEqual(polled['pipeline'], 'story')
        self.assertEqual(len(polled['results']), 3)
        # rerunning the finished job is a no-op (atomic claim)
        story_pipeline.run_job(str(row.pk))
        self.assertEqual(GeneratedShort.objects.filter(job=row).count(), 3)
        # deleting the project removes its jobs and videos
        self.assertEqual(self.client.delete(f'/api/v1/story/projects/{pid}').status_code, 204)
        self.assertFalse(GeneratedShort.objects.filter(pk=videos[0].pk).exists())
