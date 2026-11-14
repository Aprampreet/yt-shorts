import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, Mock
from django.test import TestCase, SimpleTestCase, override_settings, Client
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from .models import SourceVideo, GenerationJob, GeneratedShort
from .services.media import validate_youtube, StudioError, probe, run
from .services.analysis import candidates, select_distinct
from .services.captions import write_captions, parse_vtt, local_cues
from .services.providers import HuggingFaceProvider, EditorialAnalysis
from .services.local_transcription import parse_whisper_json
from .services.rendering import render_command, validate_output
from .tasks import generate_job, update, Cancelled


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


class APITests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')

    @patch('api.views.generate_job.delay')
    def test_create_rights_validation_and_owner_access(self, delay):
        payload = {'url':'https://youtu.be/abcdefghijk','rights_declaration':False}
        response = self.client.post('/api/v1/jobs',json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,400)
        self.assertEqual(GenerationJob.objects.count(),0)
        payload['rights_declaration'] = True
        response = self.client.post('/api/v1/jobs',json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,202,response.content)
        job_id = response.json()['id']
        self.assertEqual(response.json()['status'],'queued')
        delay.assert_called_once_with(job_id)
        other = Client(); other.get('/api/v1/session')
        self.assertEqual(other.get(f'/api/v1/jobs/{job_id}').status_code,404)
        self.assertEqual(self.client.post(f'/api/v1/jobs/{job_id}/cancel').json()['status'],'cancelled')

    def test_csrf_enforced(self):
        client = Client(enforce_csrf_checks=True)
        token = client.get('/api/v1/session').json()['csrf_token']
        data = json.dumps({'url':'https://youtu.be/abcdefghijk','rights_declaration':True})
        self.assertEqual(client.post('/api/v1/jobs',data,content_type='application/json').status_code,403)
        with patch('api.views.generate_job.delay'):
            self.assertEqual(client.post('/api/v1/jobs',data,content_type='application/json',HTTP_X_CSRFTOKEN=token).status_code,202)

    @patch('api.views.generate_job.delay',side_effect=ConnectionError())
    def test_queue_failure_is_actionable(self, delay):
        response = self.client.post('/api/v1/jobs',json.dumps({'url':'https://youtu.be/abcdefghijk','rights_declaration':True}),content_type='application/json')
        self.assertEqual(response.status_code,202)
        self.assertEqual(response.json()['error_code'],'queue_unavailable')

    def test_cancelled_claim_is_idempotent(self):
        source = SourceVideo.objects.create(rights_declaration=True)
        job = GenerationJob.objects.create(source=source,owner='owner',status='cancelled')
        generate_job(str(job.pk))
        with self.assertRaises(Cancelled):
            update(job,'rendering','Rendering',50)
        job.refresh_from_db()
        self.assertEqual(job.status,'cancelled')


@override_settings(PROCESS_TIMEOUT=120,MAX_SOURCE_DURATION=1800)
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
                 patch('api.tasks.transcribe_source', return_value=[]):
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
            with patch('api.tasks.probe') as mocked:
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


class StorageAndUploadTests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        self.tmp.cleanup()

    @patch('api.views.generate_job.delay')
    def test_upload_limits_and_private_range_downloads(self, delay):
        media = SimpleUploadedFile('owned-source.mp4',b'uploaded-original-content',content_type='video/mp4')
        response = self.client.post('/api/v1/uploads',{'file':media,'rights_declaration':'true','options':'{"target_duration":20}'})
        self.assertEqual(response.status_code,202,response.content)
        job = GenerationJob.objects.get(pk=response.json()['id'])
        source_file = Path(self.tmp.name)/'jobs'/str(job.pk)/'source'/'upload.mp4'
        self.assertEqual(source_file.read_bytes(),b'uploaded-original-content')
        output = Path(self.tmp.name)/'test.mp4'; output.write_bytes(b'0123456789')
        short = GeneratedShort.objects.create(job=job,short_number=1,start_time=0,end_time=20,duration=20,title='Owned test',video_file='test.mp4')
        path = f'/api/v1/shorts/{short.pk}/video'
        part = self.client.get(path,HTTP_RANGE='bytes=2-5')
        self.assertEqual(part.status_code,206)
        self.assertEqual(b''.join(part.streaming_content),b'2345')
        self.assertEqual(part['Content-Range'],'bytes 2-5/10')
        self.assertEqual(self.client.get(path,HTTP_RANGE='bytes=99-100').status_code,416)
        stranger = Client(); stranger.get('/api/v1/session')
        self.assertEqual(stranger.get(path).status_code,404)
        download = self.client.get(f'/api/v1/shorts/{short.pk}/download')
        self.assertIn('attachment',download['Content-Disposition'])
        self.assertEqual(b''.join(download.streaming_content),b'0123456789')
        short.video_file = '../outside.mp4'; short.save()
        self.assertEqual(self.client.get(path).status_code,404)

    def test_cleanup_only_expires_terminal_assets(self):
        from django.core.management import call_command
        source = SourceVideo.objects.create(rights_declaration=True)
        old = GenerationJob.objects.create(source=source,owner='old',status='needs_review')
        active = GenerationJob.objects.create(source=source,owner='active')
        GenerationJob.objects.filter(pk=old.pk).update(created_at=timezone.now()-__import__('datetime').timedelta(days=9))
        for job in [old,active]:
            folder = Path(self.tmp.name)/'jobs'/str(job.pk)/'source'
            folder.mkdir(parents=True); (folder/'upload.mp4').write_bytes(b'asset')
        call_command('cleanup_studio',stdout=__import__('io').StringIO())
        self.assertFalse(GenerationJob.objects.filter(pk=old.pk).exists())
        self.assertTrue(GenerationJob.objects.filter(pk=active.pk).exists())
        self.assertTrue((Path(self.tmp.name)/'jobs'/str(active.pk)/'source'/'upload.mp4').exists())
        self.assertTrue(SourceVideo.objects.filter(pk=source.pk).exists())

    @patch('yt_dlp.YoutubeDL')
    def test_download_uses_actual_container_not_title(self, ydl):
        from .services.media import download_sync as download
        folder = Path(self.tmp.name)/'source'
        def extract(url, download):
            (folder/'source.webm').write_bytes(b'container')
            return {'id':'abcdefghijk','title':'A title / with : punctuation','duration':60,'subtitles':{'en':[]}}
        ydl.return_value.__enter__.return_value.extract_info.side_effect = extract
        file, metadata = download('https://youtu.be/abcdefghijk',folder)
        self.assertFalse(ydl.call_args.args[0]['writeautomaticsub'])
        self.assertFalse(ydl.call_args.args[0]['writesubtitles'])
        self.assertEqual(ydl.call_args.args[0]['js_runtimes'], {'node': {}})
        self.assertEqual(file.name,'source.webm')
        self.assertIn('punctuation',metadata['title'])
        self.assertEqual(metadata['subtitle_tracks'],['en'])

    @patch('yt_dlp.YoutubeDL')
    def test_download_reports_restricted_source(self, ydl):
        from .services.media import download_sync
        ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception('Sign in to confirm your age')
        with self.assertRaises(StudioError) as ctx:
            download_sync('https://youtu.be/abcdefghijk', Path(self.tmp.name)/'source')
        self.assertEqual(ctx.exception.code, 'source_access_restricted')

    @patch('yt_dlp.YoutubeDL')
    def test_download_reports_connection_failure(self, ydl):
        from .services.media import download_sync
        ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception('Unable to download API page: timed out')
        with self.assertRaises(StudioError) as ctx:
            download_sync('https://youtu.be/abcdefghijk', Path(self.tmp.name)/'source')
        self.assertEqual(ctx.exception.code, 'source_connection_failed')


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
        from .services.analysis import configured_weights, WEIGHTS
        with patch.dict(os.environ,{'HIGHLIGHT_WEIGHTS':json.dumps({k:v*2 for k,v in WEIGHTS.items()})}):
            weights = configured_weights()
            self.assertAlmostEqual(sum(weights.values()),1)
            self.assertAlmostEqual(weights['hook'],.2)
        with patch.dict(os.environ,{'HIGHLIGHT_WEIGHTS':'{"hook":-1}'}), self.assertRaises(StudioError):
            configured_weights()


class RequestBoundaryTests(TestCase):
    def test_oversized_upload_is_rejected_before_parsing(self):
        with override_settings(MAX_SOURCE_BYTES=10):
            response = self.client.generic('POST','/api/v1/uploads',b'',CONTENT_LENGTH=str(2*1024*1024),content_type='multipart/form-data; boundary=test')
        self.assertEqual(response.status_code,413)
        self.assertEqual(response.json()['error'],'upload_limit')

    @patch('api.views.disk_check',side_effect=RuntimeError('private filesystem detail'))
    def test_internal_api_errors_do_not_leak_tracebacks(self, check):
        self.client.get('/api/v1/session')
        response = self.client.post('/api/v1/jobs',json.dumps({'url':'https://youtu.be/abcdefghijk','rights_declaration':True}),content_type='application/json')
        self.assertEqual(response.status_code,500)
        self.assertEqual(response.json()['error'],'internal_error')
        self.assertNotIn('private filesystem detail',response.content.decode())


class BilingualCaptionTests(SimpleTestCase):
    def test_hindi_english_hinglish_labels_and_modes(self):
        from .services.languages import detect_language, label_cues, caption_text
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
        from .services.providers import HuggingFaceProvider
        with patch.dict(os.environ,{'AI_ALLOW_BILLABLE':'false'}):
            provider = HuggingFaceProvider()
            call = Mock()
            with self.assertRaises(StudioError) as context:
                provider.request(call)
            self.assertEqual(context.exception.code,'paid_inference_disabled')
            call.assert_not_called()

    def test_unicode_captions_have_json_and_ass(self):
        from .services.languages import label_cues
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            cues = label_cues([{'start':.2,'end':2.5,'text':'Hello friend','translated_hindi':'नमस्ते दोस्त'}])
            srt, ass = write_captions(cues,folder,1,'minimal','bilingual')
            self.assertIn('नमस्ते',srt.read_text())
            self.assertIn('नमस्ते',ass.read_text())
            self.assertIn('नमस्ते',(folder/'1.json').read_text())


class SocialWorkflowTests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')
        self.source = SourceVideo.objects.create(rights_declaration=True)
        self.job = GenerationJob.objects.create(source=self.source,owner=self.client.session['studio_owner'],status='needs_review')
        self.short = GeneratedShort.objects.create(job=self.job,short_number=1,start_time=0,end_time=20,duration=20,title='Owned clip',video_file='jobs/final.mp4')

    def test_draft_requires_separate_rights_review_before_approval(self):
        from .models import ScheduledPost
        body = {'short_id':str(self.short.id),'platform':'youtube','title':'Owned clip','description':'Context','timezone':'Asia/Kolkata'}
        response = self.client.post('/api/v1/social/posts',json.dumps(body),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        post_id = response.json()['id']
        self.assertEqual(ScheduledPost.objects.count(),1)
        response = self.client.post(f'/api/v1/social/posts/{post_id}/approve')
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.client.post(f'/api/v1/shorts/{self.short.pk}/rights-review').status_code,422)
        self.assertEqual(self.client.post(f'/api/v1/shorts/{self.short.pk}/rights-review?permission_verified=true&music_verified=true').status_code,200)
        self.assertEqual(self.client.post(f'/api/v1/social/posts/{post_id}/approve').status_code,409)  # account still required
        self.assertEqual(self.client.post(f'/api/v1/social/posts/{post_id}/cancel').json()['status'],'cancelled')

    def test_timezone_slots_and_eligibility(self):
        url = '/api/v1/social/preferences'
        self.assertEqual(self.client.get(url).json()['slots'],['12:00','17:00','21:00'])
        invalid = self.client.put(url,json.dumps({'timezone':'Not/AZone','slots':['12:00']}),content_type='application/json')
        self.assertEqual(invalid.status_code,422)
        valid = self.client.put(url,json.dumps({'timezone':'Asia/Kolkata','slots':['13:30','19:00']}),content_type='application/json')
        self.assertEqual(valid.status_code,200)
        self.assertEqual(len(self.client.get('/api/v1/social/suggestions').json()),3)
        instagram = self.client.post('/api/v1/social/posts',json.dumps({'short_id':str(self.short.pk),'platform':'instagram','title':'clip','description':'text'}),content_type='application/json')
        self.assertEqual(instagram.status_code,400)
        self.assertEqual(instagram.json()['error'],'instagram_unavailable')

    def test_social_tokens_encrypt_and_youtube_session_is_reused(self):
        from cryptography.fernet import Fernet
        from .services.social import seal, unseal, youtube_upload
        from .models import ConnectedSocialAccount, ScheduledPost
        key = Fernet.generate_key().decode()
        with patch.dict(os.environ,{'SOCIAL_ENCRYPTION_KEY':key,'YOUTUBE_CLIENT_ID':'id','YOUTUBE_CLIENT_SECRET':'secret','YOUTUBE_REDIRECT_URI':'http://localhost:8000/api/v1/social/youtube/callback'}):
            token = seal({'access_token':'private-token','refresh_token':'refresh'})
            self.assertNotIn('private-token',token)
            self.assertEqual(unseal(token)['refresh_token'],'refresh')
            account = ConnectedSocialAccount.objects.create(owner=self.job.owner,platform='youtube',platform_account_id='channel',account_name='Channel',encrypted_credentials=token,token_expiry=timezone.now()+__import__('datetime').timedelta(hours=1))
            post = ScheduledPost.objects.create(owner=self.job.owner,generated_short=self.short,social_account=account,platform='youtube',title='Video',description='Description',encrypted_upload_session=seal({'uri':'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test'}))
            with tempfile.TemporaryDirectory() as temp:
                video = Path(temp)/'video.mp4'; video.write_bytes(b'1234567890')
                status = Mock(status_code=308,headers={'Range':'bytes=0-4'})
                complete = Mock(status_code=200); complete.json.return_value={'id':'abcdefghijk'}
                client = Mock(); client.put.side_effect=[status,complete]
                context = Mock(); context.__enter__=Mock(return_value=client);context.__exit__=Mock(return_value=False)
                with patch('api.services.social.httpx.Client',return_value=context):
                    self.assertEqual(youtube_upload(post,video),('abcdefghijk','unknown'))
                self.assertEqual(client.put.call_args_list[1].kwargs['headers']['Content-Range'],'bytes 5-9/10')

    def test_oauth_and_credentials_are_absent_by_default(self):
        from .services.social import authorization_url
        with patch.dict(os.environ,{'SOCIAL_ENCRYPTION_KEY':'','YOUTUBE_CLIENT_ID':''}):
            with self.assertRaises(StudioError):
                authorization_url('state')
        self.assertEqual(self.client.get('/api/v1/social/accounts').json(),[])


class TranscriptEditingTests(TestCase):
    def setUp(self):
        from .models import TranscriptSegment
        self.client.get('/api/v1/session')
        self.tmp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name)); self.override.enable()
        source = SourceVideo.objects.create(source_type='upload',rights_declaration=True)
        job = GenerationJob.objects.create(source=source,owner=self.client.session['studio_owner'],status='needs_review')
        self.short = GeneratedShort.objects.create(job=job,short_number=1,start_time=0,end_time=5,duration=5,title='Speech',video_file='jobs/test.mp4',caption_file='jobs/test.srt')
        self.segment = TranscriptSegment.objects.create(short=self.short,start_ms=500,end_ms=2200,original_text='Hello world',detected_language='en')
        folder = Path(self.tmp.name)/'jobs'/str(job.pk)/'source';folder.mkdir(parents=True)
        (folder/'upload.mp4').write_bytes(b'source retained')

    def tearDown(self):
        self.override.disable();self.tmp.cleanup()

    @patch('api.views.rerender_short.delay')
    def test_edit_persists_and_queues_render(self, enqueue):
        body = {'segments':[{'id':str(self.segment.id),'start_ms':500,'end_ms':2200,'original_text':'Hello, friend!',
                             'translated_english':'Hello, friend!','translated_hindi':'नमस्ते दोस्त'}]}
        response = self.client.put(f'/api/v1/shorts/{self.short.pk}/transcript',json.dumps(body),content_type='application/json')
        self.assertEqual(response.status_code,202,response.content)
        self.segment.refresh_from_db()
        self.assertEqual(self.segment.translated_hindi,'नमस्ते दोस्त')
        enqueue.assert_called_once_with(str(self.short.pk))
        self.assertEqual(response.json()['quality_report']['transcript_edit_status'],'rendering')

    def test_invalid_timing_and_wrong_owner(self):
        body = {'segments':[{'id':str(self.segment.id),'start_ms':4500,'end_ms':6500,'original_text':'wrong'}]}
        response = self.client.put(f'/api/v1/shorts/{self.short.pk}/transcript',json.dumps(body),content_type='application/json')
        self.assertEqual(response.status_code,422)
        other = Client();other.get('/api/v1/session')
        self.assertEqual(other.get(f'/api/v1/shorts/{self.short.pk}').status_code,404)

    def test_rendered_hindi_and_hinglish_are_burned_into_video(self):
        from .services.rendering import render_command
        from .services.languages import label_cues
        from .services.captions import write_captions
        if ' ass ' not in run(['ffmpeg','-hide_banner','-filters'])[0]:
            self.skipTest('libass unavailable')
        folder = Path(self.tmp.name)
        source = folder/'test.mp4'
        run(['ffmpeg','-nostdin','-y','-f','lavfi','-i','color=c=black:size=320x180:rate=25','-t','3',source])
        cues = label_cues([dict(start=.2,end=2.5,text='Hello friend',translated_hindi='नमस्ते दोस्त, yaar!')])
        _,ass = write_captions(cues,folder,1,'minimal','bilingual')
        output = folder/'captioned.mp4'
        command,_ = render_command(source,output,0,3,probe(source),'720','preserve',ass)
        run(command,cwd=folder)
        image = folder/'frame.png'
        run(['ffmpeg','-nostdin','-y','-ss','1','-i',output,'-frames:v','1',image])
        from PIL import Image
        # Non-black pixels in subtitle-safe lower region confirm actual shaped rendering.
        with Image.open(image) as frame:
            lower = frame.crop((0,600,frame.width,frame.height))
            self.assertGreater(sum(1 for r,g,b in lower.convert('RGB').getdata() if max(r,g,b)>60),100)


class BatchApprovalTests(TestCase):
    def test_batch_is_atomic_and_does_not_duplicate_enqueue(self):
        from .models import ConnectedSocialAccount, ScheduledPost
        client = self.client
        client.get('/api/v1/session')
        owner = client.session['studio_owner']
        source = SourceVideo.objects.create(rights_declaration=True)
        job = GenerationJob.objects.create(source=source,owner=owner,status='needs_review')
        account = ConnectedSocialAccount.objects.create(owner=owner,platform='youtube',platform_account_id='channel',account_name='Channel',encrypted_credentials='encrypted')
        posts = []
        for number in [1,2]:
            short = GeneratedShort.objects.create(job=job,short_number=number,start_time=0,end_time=20,duration=20,title=f'Clip {number}',video_file=f'{number}.mp4',rights_review_status='rights_documented' if number == 1 else 'manual_review_required')
            posts.append(ScheduledPost.objects.create(owner=owner,generated_short=short,social_account=account,platform='youtube',title=short.title,description='description'))
        url = '/api/v1/social/approve-batch'
        payload = {'post_ids':[str(p.pk) for p in posts]}
        self.assertEqual(client.post(url,json.dumps(payload),content_type='application/json').status_code,409)
        self.assertEqual(ScheduledPost.objects.filter(status='queued').count(),0)
        posts[1].generated_short.rights_review_status='rights_documented';posts[1].generated_short.save()
        with patch('api.views.publish_youtube_post.delay') as enqueue:
            response = client.post(url,json.dumps(payload),content_type='application/json')
            self.assertEqual(response.status_code,202,response.content)
            self.assertEqual(ScheduledPost.objects.filter(status='queued').count(),2)
            self.assertEqual(client.post(url,json.dumps(payload),content_type='application/json').status_code,409)


class OneClickSchedulingTests(TestCase):
    def setUp(self):
        from .models import ConnectedSocialAccount
        self.client.get('/api/v1/session')
        self.owner = self.client.session['studio_owner']
        self.tmp = tempfile.TemporaryDirectory()
        self.media = override_settings(MEDIA_ROOT=Path(self.tmp.name)); self.media.enable()
        source = SourceVideo.objects.create(source_type='upload', title='Source', rights_declaration=True)
        self.job = GenerationJob.objects.create(source=source, owner=self.owner, status='needs_review')
        self.account = ConnectedSocialAccount.objects.create(owner=self.owner, platform='youtube',
            platform_account_id='channel', account_name='Channel', encrypted_credentials='encrypted')
        for number in (1, 2, 3):
            path = Path(self.tmp.name) / f'{number}.mp4'; path.write_bytes(b'video')
            GeneratedShort.objects.create(job=self.job, short_number=number, start_time=0, end_time=20,
                duration=20, title=f'Short {number}', description='Grounded description',
                hashtags=['#Shorts'], transcript='Actual spoken words', video_file=path.name)

    def tearDown(self):
        self.media.disable(); self.tmp.cleanup()

    @patch('api.views.publish_youtube_post.delay')
    def test_one_click_sets_slots_and_is_idempotent(self, enqueue):
        from .models import ScheduledPost
        first_short = self.job.shorts.get(short_number=1)
        first_short.description = 'Grounded description\n\nSource: https://example.test/original'
        first_short.save(update_fields=['description'])
        url = f'/api/v1/jobs/{self.job.id}/schedule-batch'
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(url)
        self.assertEqual(first.status_code, 202, first.content)
        posts = first.json()['posts']
        self.assertEqual([p['status'] for p in posts], ['queued'] * 3)
        self.assertEqual([p['approval_basis'] for p in posts], ['source_declaration'] * 3)
        self.assertNotIn('Source:', posts[0]['description'])
        self.assertIn('subscribe', posts[0]['description'])
        self.assertIsNone(posts[0]['scheduled_at'])
        from datetime import datetime, timedelta
        second = datetime.fromisoformat(posts[1]['scheduled_at'])
        third = datetime.fromisoformat(posts[2]['scheduled_at'])
        self.assertEqual(third-second, timedelta(hours=5))
        self.assertAlmostEqual((second-timezone.now()).total_seconds(), 5*3600, delta=15)
        self.assertEqual(enqueue.call_count, 3)
        again = self.client.post(url)
        self.assertEqual(again.status_code, 202)
        self.assertTrue(again.json()['already_scheduled'])
        self.assertEqual(ScheduledPost.objects.count(), 3)
        self.assertEqual(enqueue.call_count, 3)

    def test_missing_account_or_declaration_blocks_upload(self):
        from .models import ScheduledPost
        self.account.connection_status = 'disconnected'; self.account.save()
        response = self.client.post(f'/api/v1/jobs/{self.job.id}/schedule-batch')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(ScheduledPost.objects.count(), 0)
        self.account.connection_status = 'connected'; self.account.save()
        self.job.source.rights_declaration = False; self.job.source.save()
        self.assertEqual(self.client.post(f'/api/v1/jobs/{self.job.id}/schedule-batch').status_code, 409)
        self.assertEqual(ScheduledPost.objects.count(), 0)

    @patch('api.views.publish_youtube_post.delay')
    def test_existing_published_short_is_not_reuploaded(self, enqueue):
        from .models import ScheduledPost
        first = self.job.shorts.get(short_number=1)
        ScheduledPost.objects.create(owner=self.owner, generated_short=first, social_account=self.account,
            platform='youtube', title=first.title, description=first.description, status='published',
            approved_at=timezone.now(), external_post_id='abcdefghijk')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'/api/v1/jobs/{self.job.id}/schedule-batch')
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual([p['status'] for p in response.json()['posts']], ['published', 'queued', 'queued'])
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(ScheduledPost.objects.count(), 3)


class PublicationStateTests(TestCase):
    def test_source_declaration_authorizes_one_click_upload(self):
        from .models import ConnectedSocialAccount, ScheduledPost
        from .tasks import publish_youtube_post
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=Path(tmp)):
            source = SourceVideo.objects.create(rights_declaration=True)
            job = GenerationJob.objects.create(source=source, owner='owner', status='needs_review')
            (Path(tmp)/'short.mp4').write_bytes(b'rendered-video')
            short = GeneratedShort.objects.create(job=job, short_number=1, start_time=0, end_time=20,
                duration=20, title='Clip', video_file='short.mp4')
            account = ConnectedSocialAccount.objects.create(owner='owner', platform='youtube',
                platform_account_id='channel', account_name='Channel', encrypted_credentials='encrypted')
            post = ScheduledPost.objects.create(owner='owner', generated_short=short, social_account=account,
                platform='youtube', title='Clip', description='Context', status='queued',
                approval_basis='source_declaration', approved_at=timezone.now())
            with patch('api.services.social.youtube_upload', return_value=('abcdefghijk','public')) as upload:
                publish_youtube_post(str(post.id))
            upload.assert_called_once()
            post.refresh_from_db()
            self.assertEqual(post.status, 'published')

    def test_private_upload_is_not_reported_as_published_and_is_idempotent(self):
        from .models import ConnectedSocialAccount, ScheduledPost
        from .tasks import publish_youtube_post
        with tempfile.TemporaryDirectory() as tmp, override_settings(MEDIA_ROOT=Path(tmp)):
            source = SourceVideo.objects.create(rights_declaration=True)
            job = GenerationJob.objects.create(source=source,owner='reviewer',status='needs_review')
            output = Path(tmp)/'short.mp4';output.write_bytes(b'rendered-video')
            short = GeneratedShort.objects.create(job=job,short_number=1,start_time=0,end_time=20,duration=20,title='Clip',video_file='short.mp4',rights_review_status='rights_documented')
            account = ConnectedSocialAccount.objects.create(owner='reviewer',platform='youtube',platform_account_id='channel',account_name='Channel',encrypted_credentials='encrypted')
            post = ScheduledPost.objects.create(owner='reviewer',generated_short=short,social_account=account,platform='youtube',title='Clip',description='Context',status='queued',approved_at=timezone.now())
            with patch('api.services.social.youtube_upload',return_value=('abcdefghijk','private')) as upload:
                publish_youtube_post(str(post.pk))
                publish_youtube_post(str(post.pk))
            upload.assert_called_once()
            post.refresh_from_db()
            self.assertEqual(post.status,'uploaded_private')
            self.assertIsNone(post.published_at)
            self.assertIn('kept this upload private',post.last_error)


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
        from .services.captions import parse_vtt
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'auto.en.vtt'
            path.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n\n00:00:00.900 --> 00:00:02.000\nHello world\n')
            cues = parse_vtt(path)
            self.assertEqual(len(cues),1)
            self.assertEqual(cues[0]['text'],'Hello world')


class GroqFormattingTests(SimpleTestCase):
    def test_metadata_hashtags_are_sanitized_and_failures_fall_back(self):
        from .services.groq import finalize_description, generate_metadata
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
        from .services.groq import transcribe_clip
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
