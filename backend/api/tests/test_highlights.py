import json
import os
from unittest.mock import patch

from django.test import SimpleTestCase

from ..editing import lexicon as lx
from ..editing.hook import hook_features, hook_overlay, is_grounded, coherence_score, payoff_score, standalone_score
from ..services.analysis import (WEIGHTS, candidates, configured_weights, replay_score, select_distinct,
                                 heatmap_peak_starts)
from ..services.media import sanitize_heatmap

SIGNALS = {'visual_samples': [], 'silence': []}


def cues_for(lines, step=6):
    return [dict(start=i*step, end=i*step+step-.5, text=t) for i, t in enumerate(lines)]


class HeatmapTests(SimpleTestCase):
    def test_sanitize_bounds_and_junk(self):
        raw = [{'start_time': i, 'end_time': i+1, 'value': i/300} for i in range(300)]
        raw += [{'start_time': 0, 'end_time': 1, 'value': float('nan')}, {'start_time': 5}, 'x', None]
        out = sanitize_heatmap(raw)
        self.assertEqual(len(out), 200)
        self.assertEqual(set(out[0]), {'s', 'e', 'v'})
        self.assertEqual(sanitize_heatmap(None), [])
        self.assertEqual(sanitize_heatmap('nope'), [])

    def test_replay_normalised_to_median_and_p95(self):
        hm = [{'s': i*10, 'e': i*10+10, 'v': v} for i, v in enumerate([.2, .2, .2, .2, .2, .2, .2, .2, .2, 1.])]
        self.assertAlmostEqual(replay_score(90, 100, hm), 1.0)
        self.assertEqual(replay_score(0, 10, hm), 0.)
        mid = replay_score(85, 95, hm)
        self.assertTrue(0 < mid < 1)
        self.assertIsNone(replay_score(0, 10, []))
        self.assertIsNone(replay_score(0, 10, None))
        flat = [{'s': i, 'e': i+1, 'v': .5} for i in range(10)]
        self.assertIsNone(replay_score(0, 5, flat))

    def test_peak_starts(self):
        hm = [{'s': i*10, 'e': i*10+10, 'v': 1. if i == 7 else .1} for i in range(10)]
        starts = heatmap_peak_starts(hm, 30, 100)
        self.assertAlmostEqual(starts[0], 75 - .35*30)

    def test_missing_heatmap_is_strict_noop(self):
        cues = cues_for(['What is the secret to focus?', 'You never need more time.', 'Here is how it works.',
                         'Then you practise daily.', 'That is why it works.'] * 4)
        plain = candidates(120, 30, cues, SIGNALS)
        for hm in (None, [], 'junk'):
            self.assertEqual(candidates(120, 30, cues, SIGNALS, heatmap=hm), plain)
        self.assertTrue(all('replay' not in c for c in plain))

    def test_heatmap_blend(self):
        cues = cues_for(['Why does this happen?', 'You will see.'] * 10)
        hm = [{'s': i*10, 'e': i*10+10, 'v': 1. if 6 <= i < 9 else .1} for i in range(12)]
        base = {(c['start'], c['end']): c for c in candidates(120, 30, cues, SIGNALS)}
        with patch.dict(os.environ, {'REPLAY_WEIGHT': '.15'}):
            blended = candidates(120, 30, cues, SIGNALS, heatmap=hm)
        hot = [c for c in blended if 'replay' in c and c['replay']['score'] == 1.0]
        self.assertTrue(hot)
        for c in hot:
            self.assertAlmostEqual(c['score'], round(.85*c['base_score'] + 15, 1), delta=.2)
            self.assertIn('most-replayed', c['replay']['label'])


class HookFixtureTests(SimpleTestCase):
    def test_english_ordering(self):
        q, _ = hook_features('Did you know most people never finish what they start?')
        plain, _ = hook_features('The meeting was held on a Tuesday afternoon.')
        filler, _ = hook_features('So, um, basically like we were just talking about stuff.')
        self.assertGreater(q, plain)
        self.assertGreater(plain, filler)

    def test_hindi_devanagari_ordering(self):
        q, why = hook_features('क्या आप जानते हैं कि सबसे बड़ी गलती क्या है?')
        filler, _ = hook_features('तो मतलब हम बस बात कर रहे थे।')
        self.assertGreater(q, filler)
        self.assertIn('question', why)

    def test_hinglish_ordering(self):
        q, _ = hook_features('Aap kabhi sochte ho ki yeh galti kyun hoti hai?')
        filler, _ = hook_features('Toh matlab hum bas yahan baithe the.')
        self.assertGreater(q, filler)

    def test_latency_and_signals(self):
        base, _ = hook_features('Why does this work?')
        slow, note = hook_features('Why does this work?', latency=3)
        self.assertLess(slow, base)
        self.assertIn('first word', note)
        rise, note = hook_features('Why does this work?', energy_rise=.3, scene_change=True)
        self.assertGreater(rise, base)

    def test_strong_question_opener_candidate_outranks_filler_opener(self):
        strong = [dict(start=0, end=5, text='Why do most startups fail? You never see this coming.'),
                  dict(start=5, end=29, text='The reason is simple. That is why it works.')]
        weak = [dict(start=0, end=5, text='So um basically like yeah.'),
                dict(start=5, end=29, text='We went there and then and it was fine')]
        a = candidates(30, 30, strong, SIGNALS)[0]
        b = candidates(30, 30, weak, SIGNALS)[0]
        self.assertGreater(a['components']['hook'], b['components']['hook'])
        self.assertGreater(a['score'], b['score'])

    def test_coherence_payoff_standalone(self):
        good, _ = coherence_score('Here is the plan. It works.', 10, 10, True)
        bad, _ = coherence_score('and then they left the', 10, 12, False)
        self.assertGreater(good, bad)
        self.assertLess(standalone_score('And then he said no')[0], standalone_score('Here is the plan')[0])
        self.assertGreater(payoff_score('We tried it. That is why it works!')[0], payoff_score('we tried it and')[0])


class GroundingTests(SimpleTestCase):
    T = 'Why do most startups fail? Founders are launching products nobody wants, and 90 percent give up.'

    def test_accepts_extractive_and_inflected(self):
        self.assertTrue(is_grounded('Why do most startups fail?', self.T))
        self.assertTrue(is_grounded('Founders launched products', self.T))  # launched ~ launching (>=5 prefix)
        self.assertTrue(is_grounded('90 percent give up', self.T))

    def test_rejects_invented(self):
        self.assertFalse(is_grounded('95 percent give up', self.T))
        self.assertFalse(is_grounded('Elon Musk says startups fail', self.T))
        self.assertFalse(is_grounded('Why startups never recover', self.T))
        self.assertFalse(is_grounded('NASA data', self.T))
        self.assertFalse(is_grounded('x' * 61, 'x' * 61))

    def test_overlay_extractive_and_bounded(self):
        out = hook_overlay(self.T)
        self.assertEqual(out['source'], 'extractive')
        self.assertLessEqual(len(out['text'].split()), 8)
        self.assertLessEqual(len(out['text']), 60)
        self.assertTrue(is_grounded(out['text'], self.T))
        self.assertIsNone(hook_overlay(''))
        hindi = 'क्या आप जानते हैं कि सबसे बड़ी गलती क्या है? यह बहुत आसान है।'
        o = hook_overlay(hindi)
        self.assertTrue(is_grounded(o['text'], hindi))


class CandidateReportTests(SimpleTestCase):
    def test_components_explained_and_no_placeholders(self):
        cues = cues_for(['Why does sleep matter for memory?', 'Researchers measured recall after rest.',
                         'You remember far more after sleeping.', 'That is why students should sleep.',
                         'Sleep also helps creativity.'] * 4)
        pool = candidates(120, 30, cues, SIGNALS)
        for c in pool:
            self.assertEqual(set(c['components']), set(WEIGHTS))
            self.assertEqual(set(WEIGHTS) - set(c['components_explained']), set())
            self.assertIn('not a virality prediction', c['disclaimer'])
            for k in ('hook', 'coherence', 'clarity', 'payoff', 'novelty'):
                self.assertNotIn(c['components'][k], (.5, .6, .45, .25))
                self.assertTrue(c['components_explained'][k])

    def test_select_distinct_still_three_and_weights_unchanged(self):
        cues = cues_for([f'Topic number {i} is discussed here with unique words{i} alpha{i}.' for i in range(16)])
        chosen = select_distinct(candidates(96, 30, cues, SIGNALS))
        self.assertEqual(len(chosen), 3)
        for a, b in zip(chosen, chosen[1:]):
            self.assertLessEqual(a['end'], b['start'])
        self.assertEqual(set(configured_weights()), set(WEIGHTS))
        self.assertEqual(len(WEIGHTS), 8)

    def test_energy_signal_used_when_available(self):
        cues = cues_for(['Why does this matter?', 'You should care.'] * 8)
        flat = dict(SIGNALS, energy=[(i*.5, .5) for i in range(240)])
        lively = dict(SIGNALS, energy=[(i*.5, .9 if i % 4 == 0 else .2) for i in range(240)])
        a = candidates(120, 30, cues, flat)[0]['components']['audio']
        b = candidates(120, 30, cues, lively)[0]['components']['audio']
        self.assertGreater(b, a)

    def test_lexicon_tokenizer_keeps_devanagari_words(self):
        self.assertEqual(lx.tokenize('क्या आप?'), ['क्या', 'आप'])
