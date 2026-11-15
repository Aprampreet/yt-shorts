"""WP8: editing presets wired into the Shorts pipeline (synthetic lavfi media, mocked providers, no network)."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from ..editing import graphics, hook, presets
from ..editing.ffmpeg_graph import Capabilities, compile as compile_timeline
from ..editing.render import render_timeline
from ..editing.shorts_compiler import compile_candidate
from ..models import GeneratedShort, GenerationJob, MediaAsset, SourceVideo
from ..services.captions import caption_groups, write_captions
from ..services.languages import label_cues
from ..services.media import StudioError, probe, run
from ..tasks import generate_job, rerender_short

NO_LIBASS = Capabilities('test', frozenset({'overlay', 'zoompan', 'xfade'}), True)
WITH_LIBASS = Capabilities('test', frozenset({'overlay', 'zoompan', 'xfade', 'ass'}), True)
ENV = {'HF_TOKEN': '', 'HF_TRANSCRIPTION_MODEL': '', 'HF_TEXT_MODEL': '', 'GROQ_API_KEY': '', 'GROQ_ENABLED': 'false',
       'CAPTION_RENDERER': 'auto'}
SILENT_GAP = (13.0, 16.0)
CUES = [
    (0.3, 2.6, 'Have you ever seen a speedrun this insane?'),
    (2.9, 5.5, 'Watch the jump at the very end.'),
    (5.8, 8.4, 'Nobody expected the boss to fall this fast.'),
    (8.7, 11.2, 'The trick is a frame perfect dodge.'),
    (11.5, 13.0, 'Then everything changed for the runner.'),
    (16.0, 18.6, 'The second run was even faster than the first one.'),
    (18.9, 21.5, 'You never see players take this route.'),
    (21.8, 24.4, 'It saves almost twelve seconds overall.'),
    (24.7, 27.3, 'That is why the community loves it.'),
    (27.6, 29.6, 'Try it yourself tonight and see.'),
]


def source_cues():
    return label_cues([dict(start=a, end=b, text=t) for a, b, t in CUES])


def make_source(path, seconds=30, size='640x360'):
    gap = f"volume='if(between(t,{SILENT_GAP[0]},{SILENT_GAP[1]}),0,1)':eval=frame"
    run(['ffmpeg', '-nostdin', '-y', '-f', 'lavfi', '-i', f'testsrc2=size={size}:rate=24',
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', str(seconds), '-af', gap,
         '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', path])


def info_for(path):
    info = probe(path)
    info['license_status'] = 'owned'
    return info


class _Media:
    """Per-test MEDIA_ROOT with one synthetic source built once per class."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._cache = tempfile.TemporaryDirectory()
        cls.master = Path(cls._cache.name) / 'master.mp4'
        make_source(cls.master)

    @classmethod
    def tearDownClass(cls):
        cls._cache.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = override_settings(MEDIA_ROOT=self.root, PROCESS_TIMEOUT=180)
        self.media.enable()
        self.env = patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.media.disable()
        self.tmp.cleanup()

    def source_in(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.master, folder / 'upload.mp4')
        return folder / 'upload.mp4'

    def job(self, owner='owner', **settings):
        source = SourceVideo.objects.create(source_type='upload', rights_declaration=True,
                                            source_metadata={'title': 'Synthetic preset footage'})
        base = {'target_duration': 8, 'require_captions': True}
        job = GenerationJob.objects.create(source=source, owner=owner, settings=dict(base, **settings))
        from ..pipeline import ensure_project
        ensure_project(job)
        self.source_in(self.root / 'jobs' / str(job.pk) / 'source')
        return job

    def run_job(self, job):
        with patch('api.pipeline.transcribe.transcribe_source', return_value=[dict(c) for c in source_cues()]):
            generate_job(str(job.pk))
        job.refresh_from_db()
        return job


@override_settings(PROCESS_TIMEOUT=180)
class PresetCatalogueTests(SimpleTestCase):
    def test_resolution_defaults_and_overrides(self):
        legacy = presets.resolve({})
        self.assertTrue(legacy.legacy)
        self.assertEqual((legacy.composition, legacy.caption_preset, legacy.grade), ('blur_fill', 'clean_commentary', 'restrained'))
        self.assertEqual(presets.resolve({'layout': 'center', 'caption_style': 'gaming'}).composition, 'center')
        self.assertEqual(presets.resolve({'caption_style': 'gaming'}).caption_preset, 'bold_gaming')
        hype = presets.resolve({'preset': 'gaming_hype'})
        self.assertEqual((hype.composition, hype.caption_preset, hype.grade, hype.theme),
                         ('dynamic_center', 'bold_gaming', 'punchy', 'gaming_hype'))
        self.assertFalse(hype.legacy)
        self.assertFalse(hype.remove_dead_air)          # switches are explicit opt-ins
        override = presets.resolve({'preset': 'gaming_hype', 'composition': 'pip', 'caption_preset': 'cinematic'})
        self.assertEqual((override.composition, override.caption_preset), ('pip', 'cinematic'))
        self.assertEqual(presets.resolve({'subtitle_mode': 'bilingual'}).caption_preset, 'bilingual')
        podcast = presets.resolve({'preset': 'gaming_hype', 'content_category': 'podcast'})
        self.assertGreater(podcast.pacing['min_gap'], hype.pacing['min_gap'])
        self.assertEqual(len(presets.catalogue()), 6)
        for name, preset in presets.PRESETS.items():
            if name != 'none':
                self.assertIn(preset.theme, __import__('api.editing.card_specs', fromlist=['THEMES']).THEMES)


class CaptionServiceTests(SimpleTestCase):
    def test_bilingual_lines_stay_stacked_with_smaller_translation(self):
        cues = label_cues([{'start': .2, 'end': 2.5, 'text': 'Hello friend how are you',
                            'translated_hindi': 'namaste dost kaise ho'}])
        groups = caption_groups(cues, 'bilingual', 'bilingual')
        self.assertEqual(groups[0]['text'], 'Hello friend how are you\nnamaste dost kaise ho')
        with tempfile.TemporaryDirectory() as tmp:
            srt, ass = write_captions(cues, Path(tmp), 1, 'minimal', 'bilingual')
            self.assertIn('Hello friend how are you\nnamaste dost kaise ho', srt.read_text())
            event = [l for l in ass.read_text().splitlines() if l.startswith('Dialogue:')][0]
            self.assertIn(r'\N{\fs45', event)          # 64 * 0.7 for the translated line
            self.assertIn('namaste dost kaise ho', event.split(r'\N{\fs45')[1])
            # PNG renderer: two blocks, translated block at ~70 % of the original size.
            _, rect, (first, second) = graphics._render_caption(groups[0]['text'], graphics.get_preset('bilingual'),
                                                                'bilingual', (720, 1280))
            self.assertIsNotNone(second)
            self.assertAlmostEqual(second.size / first.size, .7, delta=.06)

    def test_karaoke_only_with_real_word_timings_and_safe_margins(self):
        plain = [dict(start=0, end=2, text='one two three four', detected_language='en')]
        with tempfile.TemporaryDirectory() as tmp:
            _, ass = write_captions(plain, Path(tmp), 1, 'bold_gaming')
            text = ass.read_text()
            self.assertNotIn(r'\k', text)
            self.assertIn(',420,1', text.split('Style: Main')[1].splitlines()[0])   # MarginV clears the bottom UI
            timed = [dict(plain[0], words=[dict(start=0, end=.4, word='one'), dict(start=.5, end=.9, word='two'),
                                           dict(start=1.0, end=1.4, word='three'), dict(start=1.5, end=2, word='four')])]
            _, ass = write_captions(timed, Path(tmp), 2, 'bold_gaming')
            text = ass.read_text()
            self.assertIn('Style: Karaoke', text)
            self.assertIn(r'{\k50}one', text)
        self.assertEqual(caption_groups(timed, 'bold_gaming')[0]['timing'], 'word')
        self.assertEqual(caption_groups(plain, 'bold_gaming')[0]['timing'], 'approximate')


class CompilerTests(_Media, TestCase):
    def compile(self, candidate, settings, cues=None, secondary=None, renderer_caps=NO_LIBASS):
        src = self.source_in(self.root / 'jobs' / 'unit' / 'source')
        info = info_for(src)
        start, end = candidate
        local = [dict(c, start=max(c['start'], start) - start, end=min(c['end'], end) - start)
                 for c in (cues if cues is not None else source_cues()) if c['end'] > start and c['start'] < end]
        signals = {'silence': [SILENT_GAP], 'scene_boundaries': [], 'energy': []}
        with patch('api.editing.shorts_compiler.ffmpeg_capabilities', return_value=renderer_caps):
            return compile_candidate(src, info, dict(start=start, end=end), local, signals, presets.resolve(settings),
                                     workdir=self.root / 'jobs' / 'unit' / 'captions' / 'edit', number=1,
                                     source_license='owned', secondary=secondary,
                                     caption_folder=self.root / 'jobs' / 'unit' / 'captions')

    def render(self, compiled, name='out'):
        return render_timeline(compiled.timeline, self.root / f'{name}.mp4', self.root / f'work-{name}')

    def test_dead_air_removed_and_captions_remapped(self):
        capped = self.compile((9.0, 21.0), {'remove_dead_air': True, 'preset': 'minimal_premium'})
        self.assertTrue(capped.report['pacing']['capped'])          # gentle preset: at most 15 % removed
        self.assertAlmostEqual(capped.report['pacing']['removed_seconds'], 1.8, delta=.01)
        compiled = self.compile((9.0, 21.0), {'remove_dead_air': True, 'preset': 'gaming_hype', 'composition': 'center',
                                              'content_category': 'gaming'})
        pacing = compiled.report['pacing']
        self.assertGreater(pacing['removed_seconds'], 2.0)
        self.assertLess(compiled.timeline.duration, 12.0 - 2.0)
        self.assertEqual(len(compiled.timeline.video[0].clips), 2)
        starts = [c['start'] for c in compiled.cues]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(c['end'] <= compiled.timeline.duration + 1e-6 for c in compiled.cues))
        # The cue after the gap moved earlier by the removed amount.
        after = next(c for c in compiled.cues if c['text'].startswith('The second run'))
        self.assertAlmostEqual(after['start'], 16.0 - 9.0 - pacing['removed_seconds'], delta=.02)
        result = self.render(compiled, 'deadair')
        self.assertAlmostEqual(probe(result.output)['duration'], compiled.timeline.duration, delta=.15)
        self.assertTrue(all(result.report['checks'].values()), result.report)

    def test_hook_overlay_is_grounded_and_rendered(self):
        compiled = self.compile((0.0, 8.4), {'preset': 'gaming_hype', 'hook_overlay': True, 'composition': 'center'})
        info = compiled.summary['hook_overlay']
        self.assertIsNotNone(info)
        transcript = ' '.join(t for _, _, t in CUES[:3])
        self.assertTrue(hook.is_grounded(info['text'], transcript))
        for word in info['text'].replace('?', '').split():
            self.assertIn(word.lower(), transcript.lower())
        self.assertEqual(info['source'], 'extractive')
        card = next(o for o in compiled.timeline.overlays if o.kind == 'hook_title')
        self.assertTrue(Path(card.abs_path).is_file())
        self.assertLessEqual(card.end, 3.0)

    def test_png_captions_without_libass(self):
        compiled = self.compile((0.0, 8.4), {})
        self.assertEqual(compiled.summary['caption_renderer'], 'png')
        caps = [o for o in compiled.timeline.overlays if o.kind == 'caption']
        self.assertGreaterEqual(len(caps), 3)
        self.assertTrue(all(o.rect.within(compiled.timeline.captions.safe_rect) for o in caps))
        self.assertIn('Have you ever seen', compiled.srt.read_text())
        result = self.render(compiled, 'captions')
        self.assertTrue(result.report['extended_checks']['captions_in_safe_rect'])
        self.assertEqual(result.plan.caption_renderer, 'png')

    def test_ass_used_when_libass_present_and_forced_ass_needs_libass(self):
        compiled = self.compile((0.0, 8.4), {}, renderer_caps=WITH_LIBASS)
        self.assertEqual(compiled.summary['caption_renderer'], 'ass')
        self.assertTrue(compiled.timeline.captions.ass_file.endswith('1.ass'))
        plan = compile_timeline(compiled.timeline, self.root, capabilities=WITH_LIBASS)
        self.assertIn('ass=1.ass', plan.filter_script)
        with patch.dict(os.environ, {'CAPTION_RENDERER': 'ass'}), self.assertRaises(StudioError) as ctx:
            self.compile((0.0, 8.4), {})
        self.assertEqual(ctx.exception.code, 'libass_required')

    def test_devanagari_without_raqm_gives_clear_error(self):
        cues = label_cues([dict(start=.5, end=3, text='यह बहुत अच्छा है')])
        with patch('api.editing.graphics.features.check', return_value=False), self.assertRaises(StudioError) as ctx:
            self.compile((0.0, 4.0), {}, cues=cues)
        self.assertEqual(ctx.exception.code, 'text_shaping_unavailable')
        # With libass available the ASS renderer shapes Devanagari itself; no PNG guard involved.
        with patch('api.editing.graphics.features.check', return_value=False), \
             patch('api.services.captions.shutil.which', return_value=None):
            compiled = self.compile((0.0, 4.0), {}, cues=cues, renderer_caps=WITH_LIBASS)
        self.assertEqual(compiled.summary['caption_renderer'], 'ass')

    def test_split_screen_same_source_corner_and_pip(self):
        compiled = self.compile((0.0, 8.4), {'preset': 'split_screen_gaming', 'facecam_corner': 'bottom-left'})
        tl = compiled.timeline
        self.assertEqual(tl.layout.kind, 'split_v')
        face = tl.video[0].clips[0].crop.region if tl.video[0].slot == 'top' else tl.video[1].clips[0].crop.region
        self.assertLess(face[0], .05)                 # left edge of the source
        self.assertGreater(face[1] + face[3], .95)    # bottom edge of the source
        result = self.render(compiled, 'split')
        self.assertEqual((probe(result.output)['width'], probe(result.output)['height']), (720, 1280))
        pip = self.compile((0.0, 8.4), {'composition': 'pip', 'facecam_corner': 'top-left'})
        slot = pip.timeline.layout.slot('pip')
        self.assertLess(slot.rect.x, 100)
        self.assertEqual(pip.summary['composition'], 'pip')

    def test_split_screen_with_secondary_asset(self):
        folder = self.root / 'assets' / 'owner'
        folder.mkdir(parents=True)
        run(['ffmpeg', '-nostdin', '-y', '-f', 'lavfi', '-i', 'mandelbrot=size=320x240:rate=24', '-t', '3',
             '-c:v', 'libx264', '-preset', 'ultrafast', folder / 'sec.mp4'])
        secondary = dict(path='assets/owner/sec.mp4', duration=3.0, width=320, height=240, has_audio=False,
                         license_status='unknown', asset_id='3f0c1d2e-0000-4000-8000-000000000001', kind='video')
        compiled = self.compile((0.0, 8.4), {'preset': 'split_screen_gaming', 'secondary_asset_id': secondary['asset_id']},
                                secondary=secondary)
        tl = compiled.timeline
        bottom = next(t for t in tl.video if t.slot == 'bottom')
        self.assertEqual(len(bottom.clips), 3)        # 3 s clip tiled over 8.4 s
        self.assertAlmostEqual(bottom.clips[-1].end, tl.duration, delta=1e-3)
        statuses = {p['kind']: p['license_status'] for p in tl.provenance}
        self.assertEqual(statuses, {'source': 'owned', 'asset': 'unknown', 'overlay': 'generated'})
        result = self.render(compiled, 'secondary')
        self.assertEqual(result.report['qa_status'], 'flagged')
        self.assertEqual(result.report['rights_review_status'], 'manual_review_required')
        self.assertTrue(all(result.report['checks'].values()))


class RestrictedAssetAPITests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')
        self.owner = self.client.session['studio_owner']

    def asset(self, status, owner=None):
        return MediaAsset.objects.create(owner=owner or self.owner, kind='video', file='assets/x/sec.mp4',
                                         license_status=status)

    @patch('api.views.generate_job.delay')
    def test_secondary_asset_rules_at_job_creation(self, delay):
        def post(asset_id):
            payload = {'url': 'https://youtu.be/abcdefghijk', 'rights_declaration': True,
                       'options': {'preset': 'split_screen_gaming', 'secondary_asset_id': str(asset_id)}}
            return self.client.post('/api/v1/jobs', json.dumps(payload), content_type='application/json')
        response = post(self.asset('restricted').pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'asset_restricted')
        response = post(self.asset('owned', owner='someone-else').pk)
        self.assertEqual(response.json()['error'], 'asset_not_found')
        pending = self.asset('permission_pending')
        response = post(pending.pk)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()['settings']['secondary_asset_id'], str(pending.pk))
        self.assertEqual(response.json()['settings']['preset'], 'split_screen_gaming')
        self.assertEqual(response.json()['license_status'], 'verification_needed')   # source rights untouched
        self.assertEqual(GenerationJob.objects.count(), 1)


class PresetPipelineTests(_Media, TestCase):
    """Each preset renders three Shorts end-to-end through generate_job."""
    def assertShorts(self, job):
        from ..editing.ffmpeg_graph import ffmpeg_capabilities
        expect_renderer = 'ass' if ffmpeg_capabilities().libass else 'png'
        self.assertEqual(job.status, 'needs_review', job.error_message)
        self.assertEqual(job.shorts.count(), 3)
        for short in job.shorts.all():
            info = probe(self.root / short.video_file)
            self.assertEqual((info['width'], info['height']), (720, 1280))
            report = short.quality_report
            self.assertTrue(all(report['checks'].values()), report['checks'])
            self.assertIn(short.qa_status, ('passed', 'flagged'), report.get('flags'))
            self.assertEqual(short.kind, 'highlight')
            self.assertEqual(short.project_id, job.project_id)
            self.assertEqual(short.timeline['canvas']['width'], 720)
            self.assertAlmostEqual(short.timeline['duration'], short.duration, delta=1e-3)
            for key in ('pacing', 'crop_mode', 'reframe_label', 'hook_overlay_source', 'caption_renderer',
                        'rights_summary', 'timeline_summary'):
                self.assertIn(key, report)
            self.assertEqual(report['caption_renderer'], expect_renderer)
            self.assertTrue(short.caption_file)
            self.assertGreater(short.segments.count(), 0)
        return job

    def test_all_presets_render_three_shorts(self):
        for name in ('gaming_hype', 'cinematic_gaming', 'viral_commentary', 'minimal_premium', 'split_screen_gaming'):
            with self.subTest(preset=name):
                job = self.run_job(self.job(preset=name, hook_overlay=True, remove_dead_air=True, punch_ins=True,
                                            content_category='gaming'))
                self.assertShorts(job)
                summaries = [s.quality_report['timeline_summary'] for s in job.shorts.all()]
                self.assertTrue(all(s['preset'] == name for s in summaries))
                expected = presets.PRESETS[name].composition
                self.assertTrue(all(s['composition'] == expected for s in summaries), summaries)
                self.assertTrue(any(s['hook_overlay'] for s in summaries), summaries)
                from ..routes.serializers import job_data
                results = job_data(job)['results']
                self.assertTrue(all(r['timeline_summary']['preset'] == name and r['qa_status'] for r in results))

    def test_preset_none_matches_legacy_timeline(self):
        job = self.run_job(self.job())
        self.assertShorts(job)
        for short in job.shorts.all():
            tl = short.timeline
            self.assertEqual(tl['layout']['kind'], 'blur_fill')
            clip = tl['video'][0]['clips']
            self.assertEqual(len(clip), 1)
            self.assertEqual((clip[0]['crop']['mode'], clip[0]['grade']), ('fit_blur', 'restrained'))
            self.assertEqual(clip[0]['zoom'], [])
            self.assertAlmostEqual(clip[0]['src_in'], short.start_time, delta=1e-3)
            self.assertAlmostEqual(tl['duration'], short.end_time - short.start_time, delta=1e-3)
            self.assertEqual(len(tl['audio']), 1)
            self.assertEqual((tl['audio'][0]['fade_in'], tl['audio'][0]['fade_out']), (0, 0))
            self.assertEqual([o['kind'] for o in tl['overlays'] if o['kind'] != 'caption'], [])
            self.assertEqual(short.quality_report['timeline_summary']['removed_seconds'], 0)
            self.assertIsNone(short.quality_report.get('bounds_snap'))
            # Same argv semantics as the legacy render_command for this window (single input, -af chain).
            from ..editing.timeline import Timeline
            plan = compile_timeline(Timeline.model_validate(tl), None, inline=True, capabilities=NO_LIBASS)
            self.assertIn('-af', plan.argv)
            self.assertEqual(plan.argv.count('-ss'), 1)

    def test_rerender_from_stored_timeline(self):
        job = self.run_job(self.job(preset='minimal_premium', remove_dead_air=True))
        self.assertEqual(job.status, 'needs_review', job.error_message)
        short = job.shorts.first()
        before = (self.root / short.video_file).stat().st_mtime_ns
        segment = short.segments.first()
        segment.original_text = 'Edited caption text here'
        segment.save()
        rerender_short(str(short.pk))
        short.refresh_from_db()
        self.assertEqual(short.quality_report['transcript_edit_status'], 'ready', short.quality_report)
        self.assertIn('Edited caption text here', (self.root / short.caption_file).read_text())
        self.assertIn('Edited caption text here', json.dumps(short.timeline['captions']))
        self.assertNotEqual((self.root / short.video_file).stat().st_mtime_ns, before)
        self.assertAlmostEqual(probe(self.root / short.video_file)['duration'], short.duration, delta=.15)
        self.assertIn(short.qa_status, ('passed', 'flagged'))
        # A short without a saved timeline still takes the legacy path (source expired here).
        GeneratedShort.objects.filter(pk=short.pk).update(timeline={})
        shutil.rmtree(self.root / 'jobs' / str(job.pk) / 'source')
        rerender_short(str(short.pk))
        short.refresh_from_db()
        self.assertEqual(short.quality_report['transcript_edit_status'], 'source_expired')

    def test_english_captions_now_render_locally(self):
        job = self.run_job(self.job(caption_style='gaming'))
        self.assertShorts(job)
        short = job.shorts.first()
        caps = [o for o in short.timeline['overlays'] if o['kind'] == 'caption']
        self.assertTrue(caps)
        self.assertEqual(short.timeline['captions']['preset'], 'bold_gaming')
