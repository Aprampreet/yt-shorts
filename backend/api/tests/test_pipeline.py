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


class PipelineTests(TestCase):
    """Real generated media, real encode and decode; no copyrighted footage or network."""
    def test_three_real_outputs_and_cleanup(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg required')
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=Path(tmp)):
            source = SourceVideo.objects.create(source_type='upload',rights_declaration=True,
                source_metadata={'title':'Synthetic integration footage'})
            job = GenerationJob.objects.create(source=source,owner='test',settings={'target_duration':20,'require_captions':False})
            root = Path(tmp)/'jobs'/str(job.pk)
            folder = root/'source'; folder.mkdir(parents=True)
            run(['ffmpeg','-nostdin','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=24',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','62',
                 '-c:v','libx264','-preset','ultrafast','-c:a','aac',folder/'upload.mp4'])
            with patch.dict(os.environ,{'HF_TOKEN':'','HF_TRANSCRIPTION_MODEL':'','HF_TEXT_MODEL':''}), \
                 patch('api.pipeline.transcribe.transcribe_source', return_value=[]):
                generate_job(str(job.pk))
            job.refresh_from_db()
            self.assertEqual(job.status,'needs_review',job.error_message)
            self.assertEqual(job.shorts.count(),3)
            for short in job.shorts.all():
                output = Path(tmp)/short.video_file
                info = probe(output)
                self.assertEqual((info['width'],info['height']),(720,1280))
                self.assertTrue(info['has_audio'])
                self.assertEqual(short.duration,20)
                self.assertTrue(all(short.quality_report['checks'].values()))
            self.assertFalse((root/'audio').exists())
            job.started_at = timezone.now(); job.save()
            with patch('api.pipeline.ingest.probe') as mocked:
                generate_job(str(job.pk))
                mocked.assert_not_called()

    def test_animated_captions_render_and_invalid_input(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg required')
        filters = run(['ffmpeg','-hide_banner','-filters'])[0]
        if ' ass ' not in filters:
            self.skipTest('FFmpeg libass build is required for caption integration')
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder/'test.mp4'
            run(['ffmpeg','-nostdin','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=25','-t','4',source])
            cues = [dict(start=.2,end=3.5,text='Real animated captions rendered with libass.')]
            _,ass = write_captions(cues,folder,1,'gaming')
            output = folder/'output.mp4'
            command,dims = render_command(source,output,0,4,probe(source),'720','preserve',ass)
            run(command,cwd=folder)
            report = validate_output(output,4,dims,False)
            self.assertTrue(all(report['checks'].values()))
            bad = folder/'broken.mp4'; bad.write_bytes(b'not video')
            with self.assertRaises(StudioError):
                probe(bad)


class CaptionRequirementTests(TestCase):
    def test_missing_transcript_fails_instead_of_delivering_uncaptioned_audio_video(self):
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=Path(tmp)):
            source = SourceVideo.objects.create(source_type='upload',rights_declaration=True,source_metadata={'title':'Speech required test'})
            job = GenerationJob.objects.create(source=source,owner='test',settings={'target_duration':20,'require_captions':True})
            folder = Path(tmp)/'jobs'/str(job.id)/'source';folder.mkdir(parents=True)
            run(['ffmpeg','-nostdin','-y','-f','lavfi','-i','testsrc2=size=160x90:rate=24',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','4',
                 '-c:v','libx264','-preset','ultrafast','-c:a','aac',folder/'upload.mp4'])
            with patch.dict(os.environ,{'HF_TOKEN':'','HF_TRANSCRIPTION_MODEL':''}):
                generate_job(str(job.pk))
            job.refresh_from_db()
            self.assertEqual(job.status,'failed')
            self.assertEqual(job.error_code,'transcription_unavailable')
            self.assertEqual(job.shorts.count(),0)

    def test_rolling_auto_captions_are_merged(self):
        from ..services.captions import parse_vtt
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'auto.en.vtt'
            path.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n\n00:00:00.900 --> 00:00:02.000\nHello world\n')
            cues = parse_vtt(path)
            self.assertEqual(len(cues),1)
            self.assertEqual(cues[0]['text'],'Hello world')


class GroqFormattingTests(SimpleTestCase):
    def test_metadata_hashtags_are_sanitized_and_failures_fall_back(self):
        from ..services.groq import finalize_description, generate_metadata
        response = Mock()
        response.status_code = 200
        response.json.return_value = {'choices':[{'message':{'content':json.dumps({
            'title':'The mission route nobody expected', 'description':'The speaker explains the mission route.',
            'hashtags':['#Gaming','#Shorts','#Gaming','#ViceCity']})}}]}
        with patch.dict(os.environ, {'GROQ_API_KEY':'test-key','GROQ_ENABLED':'true'}), \
             patch('api.services.groq.httpx.post', return_value=response) as request:
            result = generate_metadata('The speaker explains the mission route.')
        self.assertIn('The speaker explains the mission route.', request.call_args.kwargs['json']['messages'][0]['content'])
        self.assertNotIn('Source title:', request.call_args.kwargs['json']['messages'][0]['content'])
        self.assertEqual(request.call_args.kwargs['json']['response_format']['type'], 'json_schema')
        self.assertEqual(result['hashtags'], ['#Gaming','#Shorts','#ViceCity'])
        self.assertIn('subscribe', result['description'])
        self.assertIn('comments', result['description'])
        self.assertTrue(result['description'].endswith('#Gaming #Shorts #ViceCity'))
        self.assertEqual(result['description'], finalize_description(result['description'], result['hashtags']))
        self.assertNotIn('Source:', finalize_description('A good clip.\n\nSource: https://example.test/video', ['#Shorts']))
        rejected = Mock(status_code=400)
        with patch.dict(os.environ, {'GROQ_API_KEY':'test-key','GROQ_ENABLED':'true'}), \
             patch('api.services.groq.httpx.post', side_effect=[rejected, response]) as retry:
            self.assertIsNotNone(generate_metadata('The speaker explains the mission route.'))
        self.assertEqual(retry.call_count, 2)
        self.assertEqual(retry.call_args_list[0].kwargs['json']['response_format']['type'], 'json_schema')
        self.assertEqual(retry.call_args_list[1].kwargs['json']['response_format']['type'], 'json_object')
        with patch.dict(os.environ, {'GROQ_API_KEY':'test-key','GROQ_ENABLED':'true'}), \
             patch('api.services.groq.httpx.post', side_effect=__import__('httpx').ConnectError('offline')):
            self.assertIsNone(generate_metadata('Actual words'))

    def test_word_timestamps_form_short_caption_phrases(self):
        from ..services.groq import transcribe_clip
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GROQ_API_KEY': 'test-key', 'GROQ_ENABLED': 'true'}):
            audio = Path(tmp)
            response = Mock()
            response.json.return_value = {'words': [
                {'word':'Hello','start':0.1,'end':0.4},
                {'word':'everyone','start':0.45,'end':0.9},
                {'word':'Welcome','start':1.7,'end':2.1},
                {'word':'back','start':2.15,'end':2.5}], 'segments': []}
            def make_audio(*args, **kwargs):
                (audio/'groq-1.flac').write_bytes(b'fake-audio')
            with patch('api.services.groq.run', side_effect=make_audio), patch('api.services.groq.httpx.post', return_value=response):
                cues = transcribe_clip(Path(tmp)/'source.mp4', audio, 10, 3, 1)
            self.assertEqual([cue['text'] for cue in cues], ['Hello everyone', 'Welcome back'])
            self.assertEqual(cues[0]['end'], 0.9)
            self.assertFalse((audio/'groq-1.flac').exists())
