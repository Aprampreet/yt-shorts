"""WP7: reframing and pacing algorithms. Synthetic lavfi media only."""
import math
import os
import random
import tempfile
import time
import resource
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from api.editing import pacing, reframe
from api.services.media import run


def _make(path, graph_inputs, filter_complex, seconds, extra=()):
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y']
    for spec in graph_inputs:
        args += ['-f', 'lavfi', '-i', spec]
    args += ['-filter_complex', filter_complex, '-t', str(seconds), *extra,
             '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(path)]
    run(args)
    return str(path)


class ReframeVideoTests(SimpleTestCase):
    """Moving / jumping white box on a flat grey background, 640x360."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        base = 'color=c=gray:s=640x360:d=8:r=25'
        box = 'color=c=white:s=60x60:d=8:r=25'
        cls.moving = _make(cls.dir / 'moving.mp4', [base, box], "[0][1]overlay=x='40+t*40':y=150:shortest=1", 8)
        cls.jump = _make(cls.dir / 'jump.mp4', [base, box], "[0][1]overlay=x='if(lt(t,4),60,520)':y=150:shortest=1", 8)
        cls.letterbox = _make(cls.dir / 'letterbox.mp4', ['color=c=gray:s=640x270:d=4:r=25', box],
                              "[0][1]overlay=x='40+t*40':y=100:shortest=1,pad=640:360:0:45:black", 4)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def test_moving_box_gives_monotonic_path_within_tolerance(self):
        result = reframe.reframe(self.moving, 0, 8, self.dir)
        self.assertEqual(result.mode, 'subject_follow')
        self.assertIn('not face or subject detection', result.label)
        self.assertLessEqual(len(result.keys), 32)
        self.assertGreaterEqual(result.samples, 30)
        for a, b in zip(result.safe_area, (0, 0, 1, 1)):
            self.assertAlmostEqual(a, b, places=2)
        half = result.crop_w / 2
        xs = [k.cx for k in result.keys]
        self.assertTrue(all(b >= a - .005 for a, b in zip(xs, xs[1:])), xs)
        for k in result.keys:
            truth = min(1 - half, max(half, (70 + 40 * k.t) / 640))
            if k.t >= 1:
                self.assertLess(abs(k.cx - truth), .1, (k.t, k.cx, truth))
            self.assertGreaterEqual(k.cx, half - 1e-3)
            self.assertAlmostEqual(k.cy, .5, places=2)
        self.assertGreater(xs[-1] - xs[0], .2)  # it actually follows

    def test_scene_cut_resets_path(self):
        result = reframe.reframe(self.jump, 0, 8, self.dir, scene_boundaries=[4.0, 30.0])
        self.assertEqual(result.segments, 2)
        cuts = [k for k in result.keys if k.cut]
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(cuts[0].t, 4.0, places=3)
        before = [k.cx for k in result.keys if k.t < 4]
        after = [k.cx for k in result.keys if k.t >= 4]
        self.assertTrue(before and after)
        self.assertTrue(all(x < .3 for x in before), before)
        # Without the reset the velocity clamp would need ~3 s to cross; with it the first key is already there.
        self.assertGreater(after[0], .75)
        self.assertTrue(all(x > .75 for x in after), after)

    def test_dynamic_center_stays_within_15_percent(self):
        result = reframe.reframe(self.moving, 0, 8, self.dir, mode='dynamic_center')
        self.assertTrue(all(.35 - 1e-6 <= k.cx <= .65 + 1e-6 for k in result.keys), result.key_tuples())

    def test_letterbox_safe_area_detected(self):
        result = reframe.reframe(self.letterbox, 0, 4, self.dir)
        x, y, w, h = result.safe_area
        self.assertAlmostEqual(y, 45 / 360, delta=.02)
        self.assertAlmostEqual(h, 270 / 360, delta=.02)
        self.assertAlmostEqual(result.crop_w, h * 360 * 9 / 16 / 640, places=3)
        self.assertTrue(all(abs(k.cy - .5) < .02 for k in result.keys))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            reframe.reframe(self.moving, 0, 8, self.dir, mode='face')


class ReframeSmoothingTests(SimpleTestCase):
    def _centres(self, xs, fps=4, conf=3.0):
        return [(i / fps, x, conf) for i, x in enumerate(xs)]

    def test_deadzone_suppresses_jitter(self):
        rng = random.Random(7)
        xs = [.5 + rng.uniform(-.025, .025) for _ in range(120)]
        self.assertEqual(len(set(reframe.deadzone(xs))), 1)
        segs, _ = reframe.smooth_path(self._centres(xs), bounds=(.16, .84))
        out = [x for _, x in segs[0]]
        self.assertLess(max(out) - min(out), .01)

    def test_low_confidence_holds_previous_centre(self):
        centres = self._centres([.3] * 10) + [(2.5 + i / 4, .9, 1.0) for i in range(10)]
        segs, _ = reframe.smooth_path(centres, bounds=(0, 1))
        self.assertTrue(all(abs(x - .3) < 1e-6 for _, x in segs[0]))

    def test_velocity_clamp_and_no_overshoot(self):
        times = [i / 4 for i in range(80)]
        xs = reframe.clamp_motion(times, [.2] * 4 + [.8] * 76)
        speeds = [abs(b - a) * 4 for a, b in zip(xs, xs[1:])]
        self.assertLessEqual(max(speeds), reframe.MAX_VELOCITY + 1e-9)
        self.assertLessEqual(max(xs), .8 + 1e-9)
        self.assertAlmostEqual(xs[-1], .8, places=3)

    def test_scene_cut_reset_pure(self):
        centres = self._centres([.2] * 16 + [.8] * 16)
        segs, cut_times = reframe.smooth_path(centres, cuts=[4.0], bounds=(0, 1))
        self.assertEqual(len(segs), 2)
        self.assertEqual(cut_times, [None, 4.0])
        self.assertAlmostEqual(segs[1][0][1], .8, places=6)
        keys = reframe.path_to_keys(segs, cut_times, .5, start=0)
        self.assertEqual([k.t for k in keys if k.cut], [4.0])

    def test_rdp_caps_keys_at_32(self):
        dense = [(i / 4, .5 + .3 * math.sin(i / 150) + .004 * math.sin(i * 1.7)) for i in range(2000)]
        out = reframe.simplify_segments([dense])
        self.assertLessEqual(sum(len(s) for s in out), 32)
        self.assertGreater(sum(len(s) for s in out), 4)
        many = [[(s * 2 + i / 4, .2 + .6 * (s % 2) + .05 * i) for i in range(8)] for s in range(45)]
        out = reframe.simplify_segments(many)
        self.assertLessEqual(sum(len(s) for s in out), 32)

    def test_rdp_keeps_corner(self):
        pts = [(t, 0.0) for t in range(10)] + [(t, float(t - 9)) for t in range(10, 20)]
        kept = reframe.rdp(pts, .01)
        self.assertIn((9, 0.0), kept)
        self.assertEqual(len(kept), 3)

    def test_best_window_plateau_is_centred(self):
        profile = [0.0] * 40 + [1.0] * 4 + [0.0] * 52
        offset, conf = reframe.best_window(profile, 30)
        self.assertAlmostEqual(offset, 42, delta=1)
        self.assertGreater(conf, reframe.CONFIDENCE_MIN)
        self.assertEqual(reframe.best_window([0.0] * 96, 30)[1], 0.0)

    def test_split_screen_regions(self):
        for corner in reframe.SPLIT_CORNERS:
            r = reframe.split_screen_regions(1920, 1080, corner)
            fx, fy, fw, fh = r['facecam']
            self.assertTrue(0 <= fx and fx + fw <= 1 + 1e-3 and 0 <= fy and fy + fh <= 1 + 1e-3, r)
            self.assertAlmostEqual(fw * 1920 / (fh * 1080), 1080 / (1920 * .35), places=2)
            gx, gy, gw, gh = r['gameplay']
            self.assertAlmostEqual(gw * 1920 / (gh * 1080), 1080 / (1920 * .65), places=2)
            self.assertEqual(r['source'], 'preset')
        self.assertLess(reframe.split_screen_regions(1920, 1080, 'top-left')['facecam'][0], .01)
        self.assertGreater(reframe.split_screen_regions(1920, 1080, 'bottom-right')['facecam'][1], .5)
        user = reframe.split_screen_regions(1920, 1080, override=(.6, .05, .3, .3))
        self.assertEqual(user['source'], 'user')
        self.assertGreaterEqual(user['facecam'][0], .6 - 1e-3)
        with self.assertRaises(ValueError):
            reframe.split_screen_regions(1920, 1080, 'middle')
        with self.assertRaises(ValueError):
            reframe.split_screen_regions(1920, 1080, override=(0, 0, 0, .5))


def _words(spec):
    """spec: [(start, end, text)] -> word dicts."""
    return [{'start': s, 'end': e, 'text': t} for s, e, t in spec]


class PacingTests(SimpleTestCase):
    def test_keep_intervals_never_cuts_inside_words(self):
        words = _words([(0, .4, 'So'), (.5, 1.0, 'here'), (2.4, 2.8, 'is'), (2.9, 3.3, 'the'),
                        (4.6, 5.0, 'plan'), (5.1, 6.0, 'today.'), (7.5, 8.0, 'Okay'), (8.1, 9.6, 'then')])
        res = pacing.keep_intervals(0, 10, words, silence=None, min_gap=.6)
        self.assertTrue(res.removed)
        for lo, hi in res.removed:
            for w in words:
                self.assertFalse(w['start'] < hi and w['end'] > lo, (lo, hi, w))
                # 0.12 s padding to the nearest word edge
                self.assertTrue(lo >= w['end'] + .12 - 1e-6 or hi <= w['start'] - .12 + 1e-6, (lo, hi, w))
        kept = sum(e - s for s, e in res.intervals)
        self.assertAlmostEqual(kept + res.removed_seconds, 10, places=2)
        self.assertLessEqual(res.removal_ratio, .25)

    def test_removal_capped_at_25_percent(self):
        words = _words([(0, .5, 'one'), (3.5, 4.0, 'two'), (7.0, 7.5, 'three'), (10.5, 11.0, 'four')])
        res = pacing.keep_intervals(0, 11, words, min_gap=.5)
        self.assertTrue(res.capped)
        self.assertLessEqual(res.removed_seconds, .25 * 11 + 1e-3)
        self.assertAlmostEqual(res.removal_ratio, .25, places=2)

    def test_silence_gate_and_internal_only(self):
        words = _words([(1, 2, 'a'), (4, 5, 'b'), (8, 9, 'c')])
        # Only the 5-8 gap is actually silent; the 2-4 gap has music under it.
        res = pacing.keep_intervals(0, 12, words, silence=[(5.1, 7.9), (9.5, 12)])
        self.assertEqual(len(res.removed), 1)
        lo, hi = res.removed[0]
        self.assertGreaterEqual(lo, 5.2 - 1e-6)
        self.assertLessEqual(hi, 7.8 + 1e-6)
        self.assertEqual(res.intervals[-1][1], 12)  # trailing silence is not internal

    def test_skip_without_transcript(self):
        res = pacing.keep_intervals(0, 10, [], silence=[(2, 5)])
        self.assertEqual(res.skipped, 'no_transcript')
        self.assertEqual(res.intervals, [(0, 10)])
        res = pacing.keep_intervals(0, 10, [], silence=[(2, 4)], require_transcript=False)
        self.assertIsNone(res.skipped)
        self.assertEqual(res.removed, [(2.12, 3.88)])

    def test_pause_after_question_and_before_punchline_kept(self):
        words = _words([(0, .5, 'Guess'), (.6, 1.0, 'what?'), (3.0, 3.5, 'Nothing.'),
                        (3.6, 4.0, 'We'), (4.1, 4.5, 'waited'), (4.6, 5.0, 'for'), (5.1, 5.5, 'hours'),
                        (5.6, 6.0, 'and'), (6.1, 6.5, 'hours,'), (8.5, 9.0, 'and'), (9.1, 9.5, 'then'),
                        (9.6, 10.0, 'more'), (10.1, 10.5, 'waiting'), (10.6, 11.0, 'came'), (11.1, 11.5, 'too'),
                        (13.5, 14.0, 'It'), (14.1, 14.5, 'worked.')])
        res = pacing.keep_intervals(0, 15, words, max_removal=.5)
        reasons = {r for _, _, r in res.protected}
        self.assertEqual(reasons, {'after_question', 'before_punchline'})
        kept_pause = lambda a, b: (b - a) - sum(min(b, hi) - max(a, lo) for lo, hi in res.removed if hi > a and lo < b)
        self.assertAlmostEqual(kept_pause(1.0, 3.0), pacing.DEFAULT_DRAMATIC_PAUSE, places=2)
        self.assertAlmostEqual(kept_pause(11.5, 13.5), pacing.DEFAULT_DRAMATIC_PAUSE, places=2)
        self.assertAlmostEqual(kept_pause(6.5, 8.5), .24, places=2)  # ordinary pause trimmed to padding

    def test_remap_is_monotonic_and_items_follow(self):
        words = _words([(0, .4, 'a'), (2.0, 2.4, 'b'), (5.0, 5.5, 'c.'), (6.0, 6.3, 'd')])
        res = pacing.keep_intervals(0, 7, words, min_gap=.5)
        grid = [i / 20 for i in range(-10, 160)]
        mapped = [pacing.remap(t, res.intervals) for t in grid]
        self.assertTrue(all(b >= a for a, b in zip(mapped, mapped[1:])))
        self.assertAlmostEqual(mapped[-1], res.kept_seconds, places=3)
        self.assertEqual(pacing.remap(-1, res.intervals), 0)
        cues = [{'start': 0, 'end': 2.4, 'text': 'a b', 'words': words[:2]},
                {'start': 5.0, 'end': 6.3, 'text': 'c. d', 'words': words[2:]}]
        out = pacing.remap_items(cues, res.intervals)
        self.assertEqual(len(out), 2)
        starts = [c['start'] for c in out] + [w['start'] for c in out for w in c['words']]
        self.assertEqual([w['start'] for c in out for w in c['words']],
                         sorted(w['start'] for c in out for w in c['words']))
        self.assertTrue(all(c['end'] >= c['start'] for c in out))
        self.assertLessEqual(max(c['end'] for c in out), res.kept_seconds + 1e-6)
        for orig, new in zip(words, [w for c in out for w in c['words']]):
            self.assertAlmostEqual(new['end'] - new['start'], orig['end'] - orig['start'], places=3)
        self.assertTrue(all(s >= 0 for s in starts))

    def test_snap_bounds_prefers_punctuation_end(self):
        cues = [{'start': 5.0, 'end': 8.9, 'text': 'we tried it'},
                {'start': 9.0, 'end': 11.0, 'text': 'and that failed.'},
                {'start': 11.2, 'end': 14.0, 'text': 'It never works.'},
                {'start': 14.3, 'end': 19.6, 'text': 'But then something'},
                {'start': 19.8, 'end': 31.0, 'text': 'changed for good and'},
                {'start': 31.2, 'end': 33.0, 'text': 'everyone noticed.'}]
        b = pacing.snap_bounds(10.2, 30.0, cues)
        self.assertEqual(b.start, 11.2)          # sentence start beats the nearer mid-sentence cue
        self.assertEqual(b.start_reason, 'sentence_start')
        self.assertEqual(b.end, 33.0)            # punctuated end 3 s away beats unpunctuated end 1 s away
        self.assertEqual(b.end_reason, 'sentence_end')
        b = pacing.snap_bounds(10.2, 30.0, cues, scene_boundaries=[10.9, 33.3])
        self.assertEqual(b.start, 11.2)  # 10.9 is inside the previous cue: ignored
        self.assertEqual((b.end, b.end_reason), (33.3, 'sentence_end+scene'))
        b = pacing.snap_bounds(10.2, 30.0, cues, scene_boundaries=[11.05])
        self.assertEqual((b.start, b.start_reason), (11.05, 'sentence_start+scene'))
        b = pacing.snap_bounds(5.0, 20.0, [], scene_boundaries=[5.3], duration=18)
        self.assertEqual((b.start, b.end), (5.3, 18))

    def test_punch_points_spacing_and_reasons(self):
        cues = [{'start': 0.4, 'end': 3.0, 'text': 'This is the biggest secret.'},
                {'start': 3.2, 'end': 9.0, 'text': 'Nobody tells you this, it is huge.'},
                {'start': 9.2, 'end': 20.0, 'text': 'And it actually works every time.'}]
        pts = pacing.punch_points(0, 20, cues, rms=[(13.0, -8.0), (17.0, -9.0)], min_spacing=6)
        times = [p['t'] for p in pts]
        self.assertEqual(pts[0]['reason'], 'hook')
        self.assertAlmostEqual(pts[0]['t'], .4, places=3)
        self.assertTrue(all(b - a >= 6 - 1e-6 for a, b in zip(times, times[1:])), times)
        self.assertIn('emphasis', {p['reason'] for p in pts})
        self.assertTrue(all(p['t'] < p['end'] <= p['t'] + 3 + 1e-6 for p in pts))
        keys = pacing.zoom_keys(pts, 0)
        self.assertEqual(keys[0], (0, 1.0))
        self.assertTrue(all(b[0] >= a[0] for a, b in zip(keys, keys[1:])))

    def test_audio_rms_peak_from_astats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'tone.m4a'
            run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-f', 'lavfi',
                 '-i', 'sine=f=440:d=6', '-af', "volume='if(between(t,3,3.5),1,0.03)':eval=frame",
                 '-c:a', 'aac', str(path)])
            windows = pacing.audio_rms_windows(str(path), 0, 6)
            self.assertGreaterEqual(len(windows), 10)
            peaks = pacing.rms_peaks(windows)
            self.assertEqual(len(peaks), 1, windows)
            self.assertAlmostEqual(peaks[0][0], 3.25, delta=.3)


@skipUnless(os.getenv('WP7_PERF'), 'set WP7_PERF=1 to time sampling of a synthetic 60 s 1080p clip')
class ReframePerformanceTests(SimpleTestCase):
    def test_sampling_60s_1080p(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = _make(Path(tmp) / 'long.mp4', ['color=c=gray:s=1920x1080:d=60:r=30',
                                                  'color=c=white:s=200x200:d=60:r=30'],
                        "[0][1]overlay=x='100+t*25':y=400:shortest=1", 60, extra=('-crf', '23'))
            wall, c0 = time.monotonic(), resource.getrusage(resource.RUSAGE_CHILDREN)
            result = reframe.reframe(src, 0, 60, tmp)
            c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = (c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime)
            print(f'\nWP7 sampling 60s 1080p: wall {time.monotonic() - wall:.2f}s, child CPU {cpu:.2f}s, '
                  f'timings {result.timings}')
            self.assertEqual(result.samples, 240)
            self.assertLess(cpu, 3.0)
