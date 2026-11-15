import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from django.test import TestCase, SimpleTestCase, override_settings, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from ..models import SourceVideo, GenerationJob, GeneratedShort
from ..services.media import validate_youtube, StudioError, probe, run
from ..services.analysis import candidates, select_distinct
from ..services.captions import write_captions, parse_vtt, local_cues
from ..services.providers import HuggingFaceProvider, EditorialAnalysis
from ..services.local_transcription import parse_whisper_json
from ..services.rendering import render_command, validate_output
from ..tasks import generate_job, update, Cancelled


class MediaLogicTests(SimpleTestCase):
    def test_url_validation_and_ssrf(self):
        for url in ['http://youtube.com/watch?v=abcdefghijk', 'https://evil.test/video',
                    'https://youtube.com.evil.test/watch?v=abcdefghijk', 'https://127.0.0.1/a',
                    'https://youtube.com/watch?v=abcdefghijk&list=anything',
                    'https://user@youtube.com/watch?v=abcdefghijk', 'https://youtube.com:8443/watch?v=abcdefghijk']:
            with self.subTest(url=url), self.assertRaises(StudioError):
                validate_youtube(url)
        for url in ['https://youtu.be/abcdefghijk', 'https://www.youtube.com/shorts/abcdefghijk']:
            self.assertEqual(validate_youtube(url), 'https://www.youtube.com/watch?v=abcdefghijk')

    def test_clamping_short_and_diversity(self):
        signals = {'visual_samples': [], 'silence': []}
        small = select_distinct(candidates(8,30,[],signals))
        self.assertEqual([(c['start'],c['end']) for c in small], [(0,8)])
        chosen = select_distinct(candidates(96,30,[],signals))
        self.assertEqual(len(chosen),3)
        for a,b in zip(chosen,chosen[1:]):
            self.assertLessEqual(a['end'],b['start'])
        self.assertTrue(all(0 <= c['score'] <= 100 for c in chosen))

    def test_repeated_dialogue(self):
        pool = [dict(start=i*30, end=(i+1)*30, score=90-i, transcript='the same exact dialogue') for i in range(3)]
        self.assertEqual(len(select_distinct(pool)),1)

    def test_subtitles_and_four_styles(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            vtt = folder/'source.vtt'
            vtt.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nHello <b>world</b>.\n')
            cues = parse_vtt(vtt)
            self.assertEqual(cues[0]['text'], 'Hello world.')
            clipped = local_cues(cues,2,5)
            self.assertEqual(clipped[0]['start'],0)
            styles = set()
            for style in ['gaming','minimal','cinematic','dynamic']:
                srt, ass = write_captions(clipped, folder,1,style)
                self.assertIn('00:00:00,000 --> 00:00:02,000',srt.read_text())
                styles.add(ass.read_text())
            self.assertEqual(len(styles),4)

    def test_local_transcription_timestamps_are_parsed_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'transcript.json'
            path.write_text(json.dumps({'result': {'language': 'hi'}, 'transcription': [
                {'offsets': {'from': 1500, 'to': 3500}, 'text': ' नमस्ते '},
                {'offsets': {'from': 4900, 'to': 6900}, 'text': ' दुनिया '},
            ]}))
            cues = parse_whisper_json(path, 5)
            self.assertEqual([(cue['start'],cue['end'],cue['text']) for cue in cues],
                             [(1.5,3.5,'नमस्ते'),(4.9,5.0,'दुनिया')])
            self.assertTrue(all(cue['detected_language'] == 'hi' for cue in cues))

    def test_render_arguments_fps_and_safe_paths(self):
        cmd, dims = render_command(Path('/tmp/a;touch bad.mp4'),Path('/tmp/out.mp4'),0,20,
            dict(fps=23.976,height=720,has_audio=True),'1080','preserve')
        self.assertEqual(cmd[cmd.index('-r')+1],'23.976000')
        self.assertEqual(dims,(720,1280))
        self.assertIn('/tmp/a;touch bad.mp4',cmd)
        self.assertNotIn('shell',cmd)
        self.assertIn('loudnorm',cmd[cmd.index('-af')+1])

    def test_provider_quota_and_timeout_are_explicit(self):
        provider = HuggingFaceProvider()
        provider.max_calls = 1
        response = Mock(status_code=402)
        error = RuntimeError('quota'); error.response = response
        with patch.dict(os.environ,{'AI_ALLOW_BILLABLE':'true'}), self.assertRaises(StudioError) as ctx:
            provider.request(Mock(side_effect=error))
        self.assertEqual(ctx.exception.code,'ai_unavailable')
        with patch.dict(os.environ,{'AI_ALLOW_BILLABLE':'true'}), self.assertRaises(StudioError) as ctx:
            provider.request(lambda: None)
        self.assertEqual(ctx.exception.code,'ai_budget')
        provider = HuggingFaceProvider()
        with patch.dict(os.environ,{'AI_MAX_RETRIES':'0','AI_ALLOW_BILLABLE':'true'}), self.assertRaises(StudioError) as ctx:
            provider.request(Mock(side_effect=TimeoutError()))
        self.assertEqual(ctx.exception.code,'ai_timeout')


class BoundsTests(SimpleTestCase):
    def test_subprocess_deadline_terminates_process(self):
        import sys
        import time
        started = time.monotonic()
        with self.assertRaises(StudioError) as ctx:
            run([sys.executable,'-c','import time; time.sleep(10)'],timeout=.1)
        self.assertEqual(ctx.exception.code,'processing_timeout')
        self.assertLess(time.monotonic()-started,3)

    def test_configurable_normalized_weights(self):
        from ..services.analysis import configured_weights, WEIGHTS
        with patch.dict(os.environ,{'HIGHLIGHT_WEIGHTS':json.dumps({k:v*2 for k,v in WEIGHTS.items()})}):
            weights = configured_weights()
            self.assertAlmostEqual(sum(weights.values()),1)
            self.assertAlmostEqual(weights['hook'],.2)
        with patch.dict(os.environ,{'HIGHLIGHT_WEIGHTS':'{"hook":-1}'}), self.assertRaises(StudioError):
            configured_weights()


class BilingualCaptionTests(SimpleTestCase):
    def test_hindi_english_hinglish_labels_and_modes(self):
        from ..services.languages import detect_language, label_cues, caption_text
        self.assertEqual(detect_language('Hello, how are you?'),'en')
        self.assertEqual(detect_language('आप कैसे हैं?'),'hi')
        self.assertEqual(detect_language('Yeh game bahut achha hai, bro!'),'mixed')
        cues = label_cues([{'start':0,'end':2,'text':'Hello','translated_hindi':'नमस्ते'}])
        self.assertEqual(caption_text(cues[0],'bilingual'),'Hello\nनमस्ते')
        self.assertEqual(caption_text(cues[0],'hindi'),'नमस्ते')
        self.assertEqual(caption_text(cues[0],'original'),'Hello')
        self.assertEqual(caption_text(cues[0],'english'),'Hello')
        self.assertEqual(caption_text(label_cues([{'start':0,'end':2,'text':'नमस्ते'}])[0],'english'),'')

    def test_no_paid_inference_without_explicit_enable(self):
        from ..services.providers import HuggingFaceProvider
        with patch.dict(os.environ,{'AI_ALLOW_BILLABLE':'false'}):
            provider = HuggingFaceProvider()
            call = Mock()
            with self.assertRaises(StudioError) as context:
                provider.request(call)
            self.assertEqual(context.exception.code,'paid_inference_disabled')
            call.assert_not_called()

    def test_unicode_captions_have_json_and_ass(self):
        from ..services.languages import label_cues
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cues = label_cues([{'start':.2,'end':2.5,'text':'Hello friend','translated_hindi':'नमस्ते दोस्त'}])
            srt, ass = write_captions(cues,folder,1,'minimal','bilingual')
            self.assertIn('नमस्ते',srt.read_text())
            self.assertIn('नमस्ते',ass.read_text())
            self.assertIn('नमस्ते',(folder/'1.json').read_text())
