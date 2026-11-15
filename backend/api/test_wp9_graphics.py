import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageFont, features

from api.editing import graphics
from api.editing.card_specs import (THEMES, Callout, ComparisonCard, FactCard, HookTitle, LabelValue,
                                    NumberedList, TitleCard, get_theme)
from api.services.media import StudioError

CANVAS = (1080, 1920)
HINDI = 'यह एक परीक्षण है: क्षत्रिय और श्रेष्ठ'
RAQM = bool(features.check('raqm'))


def safe_ok(rect):
    x, y, w, h = rect
    return x >= 60 and y >= 220 and x + w <= 1080 - 140 and y + h <= 1920 - 420


class CardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def check(self, spec, theme='gaming_hype'):
        r = graphics.render_card(spec, theme, CANVAS, self.out)
        for p in r.paths:
            img = Image.open(p)
            self.assertEqual(img.mode, 'RGBA')
            self.assertEqual(img.size, (r.rect[2], r.rect[3]))
            self.assertIsNotNone(img.getchannel('A').getbbox())
        self.assertTrue(safe_ok(r.rect), r.rect)
        return r

    def specs(self):
        return [
            FactCard(headline='Cyberpunk 2077 sold 30 million copies', body='The game recovered after patch 2.0 '
                     'and the Phantom Liberty expansion.', source_label='CD Projekt'),
            ComparisonCard(left=LabelValue(label='PS5', value='4K 60'), right=LabelValue(label='Xbox', value='4K 60'),
                           verdict='Dead heat'),
            Callout(text='Watch the minimap here', anchor=(0.9, 0.95)),
            NumberedList(items=['One', 'Two is a bit longer', 'Three'], reveal='stagger'),
            TitleCard(title='The Rise of Indie Games', kicker='Part one'),
            HookTitle(text='You will not believe this clip'),
        ]

    def test_each_card_all_themes(self):
        for name in THEMES:
            for spec in self.specs():
                self.check(spec, name)
        self.assertEqual(set(THEMES), {'gaming_hype', 'cinematic_gaming', 'viral_commentary', 'minimal_premium',
                                       'split_screen_gaming'})

    def test_deterministic(self):
        spec = self.specs()[0]
        a = graphics.render_card(spec, 'gaming_hype', CANVAS, self.out)
        b = graphics.render_card(spec, 'gaming_hype', CANVAS, self.out)
        self.assertEqual(a.rect, b.rect)
        self.assertEqual(a.paths, b.paths)

    def test_numbered_list_stagger(self):
        r = self.check(NumberedList(items=list('abcde'), reveal='stagger'))
        self.assertEqual(len(r.paths), 5)
        self.assertEqual(r.reveal, 'stagger')
        counts = [Image.open(p).getchannel('A').histogram()[255] for p in r.paths]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(len(graphics.render_card(NumberedList(items=['x', 'y'], reveal='all'),
                                                  'minimal_premium', CANVAS, self.out).paths), 1)

    def test_long_text_wraps_and_fits(self):
        spec = FactCard(headline='W' * 20 + ' ' + 'long headline words ' * 3, body='lorem ipsum dolor ' * 13,
                        source_label='x' * 40)
        r = self.check(spec)
        block = graphics.fit_text('very long words ' * 10, 700, 300, 64, 30, True, 4)
        self.assertGreater(len(block.lines), 1)
        self.assertLessEqual(block.width, 700)
        self.assertLessEqual(block.height, 300)
        # single unbreakable word still never exceeds the box
        block = graphics.fit_text('W' * 60, 500, 200, 80, 30, True, 3)
        self.assertLessEqual(block.width, 500)

    def test_validation_limits(self):
        with self.assertRaises(ValueError):
            NumberedList(items=list('abcdef'))
        with self.assertRaises(ValueError):
            Callout(text='x', anchor=(1.5, 0.2))
        with self.assertRaises(ValueError):
            HookTitle(text='x' * 200)
        with self.assertRaises(ValueError):
            get_theme('nope')

    def test_callout_follows_anchor_within_safe(self):
        low = graphics.render_card(Callout(text='here', anchor=(0.5, 0.4)), 'gaming_hype', CANVAS, self.out)
        self.assertAlmostEqual(low.rect[0] + low.rect[2] / 2, 540, delta=2)
        self.assertTrue(safe_ok(graphics.render_card(Callout(text='edge', anchor=(1, 1)),
                                                     'gaming_hype', CANVAS, self.out).rect))

    def test_other_canvas_scales(self):
        r = graphics.render_card(self.specs()[0], 'gaming_hype', (720, 1280), self.out)
        x, y, w, h = r.rect
        self.assertTrue(x >= 40 and y >= 147 and x + w <= 720 - 93 and y + h <= 1280 - 280 + 1)


class ShapingTests(unittest.TestCase):
    def test_guard_raises_without_raqm(self):
        with mock.patch.object(features, 'check', return_value=False):
            with self.assertRaises(StudioError) as ctx:
                graphics.ensure_shaping(HINDI)
            self.assertEqual(ctx.exception.code, 'text_shaping_unavailable')
            self.assertIn('fribidi', ctx.exception.message)
            graphics.ensure_shaping('plain latin')  # latin is always allowed
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(StudioError):
                    graphics.render_card(HookTitle(text=HINDI), 'gaming_hype', CANVAS, tmp)
                with self.assertRaises(StudioError):
                    graphics.render_caption_pngs([(0, 1, HINDI)], 'bold_gaming', 'hindi', CANVAS, tmp)

    def test_guard_passes_with_raqm(self):
        with mock.patch.object(features, 'check', return_value=True):
            graphics.ensure_shaping(HINDI)

    @unittest.skipUnless(RAQM, 'raqm (fribidi) not available')
    def test_shaped_differs_from_basic(self):
        shaped = graphics._font(True, False, 60, False).getlength(HINDI)
        basic = graphics._font(True, False, 60, True).getlength(HINDI)
        self.assertNotEqual(shaped, basic)

    def test_bundled_fonts_load(self):
        for deva in (False, True):
            for bold in (False, True):
                ImageFont.truetype(graphics._font_file(deva, bold), 30)


class CaptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def test_each_preset(self):
        groups = [(0.0, 1.5, 'this is the first phrase'), (1.5, 3.0, 'and a much much longer second phrase here ok')]
        for preset in graphics.PRESETS:
            res = graphics.render_caption_pngs(groups, preset, 'original', CANVAS, self.out)
            self.assertEqual([(r['start'], r['end']) for r in res], [(0.0, 1.5), (1.5, 3.0)])
            for r in res:
                img = Image.open(r['png'])
                self.assertEqual(img.size, tuple(r['rect'][2:]))
                bbox = img.getchannel('A').getbbox()
                self.assertIsNotNone(bbox)
                if graphics.PRESETS[preset].panel is None:
                    # nothing touches the PNG edge (no clipping); panel presets fill the PNG by design
                    self.assertGreater(bbox[0], 0)
                    self.assertGreater(bbox[1], 0)
                    self.assertLess(bbox[2], img.width)
                    self.assertLess(bbox[3], img.height)
                self.assertTrue(safe_ok(r['rect']), (preset, r['rect']))

    def test_legacy_names_map(self):
        res = graphics.render_caption_pngs([(0, 1, 'hello')], 'gaming', 'original', CANVAS, self.out)
        self.assertEqual(len(res), 1)
        with self.assertRaises(ValueError):
            graphics.render_caption_pngs([(0, 1, 'hi')], 'nope', 'original', CANVAS, self.out)

    def test_long_caption_fits(self):
        res = graphics.render_caption_pngs([(0, 2, 'extraordinarily ' * 12)], 'bold_gaming', 'original', CANVAS, self.out)
        self.assertLessEqual(res[0]['rect'][2], 800 + 40)
        self.assertTrue(safe_ok(res[0]['rect']))

    def test_bilingual_two_lines_hierarchy(self):
        preset = graphics.PRESETS['bilingual']
        a, b, _ = graphics._caption_layout('Hello brave world\nHola mundo valiente', preset, 'bilingual', CANVAS)
        self.assertIsNotNone(b)
        self.assertEqual(b.size, round(a.size * 0.7))
        self.assertGreater(a.size, b.size)
        res = graphics.render_caption_pngs([(0, 2, 'Hello brave world\nHola mundo valiente')], 'bilingual',
                                           'bilingual', CANVAS, self.out)
        single = graphics.render_caption_pngs([(0, 2, 'Hello brave world')], 'bilingual', 'original', CANVAS, self.out)
        self.assertGreater(res[0]['rect'][3], single[0]['rect'][3])
        self.assertTrue(safe_ok(res[0]['rect']))

    def test_word_highlight_states(self):
        words = [('one', 0.0, 0.4), ('two', 0.5, 0.9), ('three', 1.0, 1.4)]
        res = graphics.render_caption_pngs([(0.0, 1.5, 'one two three')], 'dynamic_creator', 'original',
                                           CANVAS, self.out, word_timings=[words])
        self.assertEqual([r['word_index'] for r in res], [0, 1, 2])
        self.assertEqual(res[0]['start'], 0.0)
        self.assertEqual(res[-1]['end'], 1.5)
        self.assertEqual(res[0]['end'], res[1]['start'])
        self.assertEqual({tuple(r['rect']) for r in res}.__len__(), 1)  # identical layout across states
        self.assertEqual(len({r['png'] for r in res}), 3)
        # mismatched timings fall back to a single PNG
        bad = graphics.render_caption_pngs([(0.0, 1.5, 'one two three')], 'dynamic_creator', 'original',
                                           CANVAS, self.out, word_timings=[words[:2]])
        self.assertEqual(len(bad), 1)
        self.assertIsNone(bad[0]['word_index'])

    @unittest.skipUnless(RAQM, 'raqm (fribidi) not available')
    def test_devanagari_caption_renders(self):
        res = graphics.render_caption_pngs([(0, 2, HINDI)], 'clean_commentary', 'hindi', CANVAS, self.out)
        self.assertIsNotNone(Image.open(res[0]['png']).getchannel('A').getbbox())
