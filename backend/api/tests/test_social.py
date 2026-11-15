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


class SocialWorkflowTests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')
        self.source = SourceVideo.objects.create(rights_declaration=True)
        self.job = GenerationJob.objects.create(source=self.source,owner=self.client.session['studio_owner'],status='needs_review')
        self.short = GeneratedShort.objects.create(job=self.job,short_number=1,start_time=0,end_time=20,duration=20,title='Owned clip',video_file='jobs/final.mp4')

    def test_draft_requires_separate_rights_review_before_approval(self):
        from ..models import ScheduledPost
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
        from ..services.social import seal, unseal, youtube_upload
        from ..models import ConnectedSocialAccount, ScheduledPost
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
        from ..services.social import authorization_url
        with patch.dict(os.environ,{'SOCIAL_ENCRYPTION_KEY':'','YOUTUBE_CLIENT_ID':''}):
            with self.assertRaises(StudioError):
                authorization_url('state')
        self.assertEqual(self.client.get('/api/v1/social/accounts').json(),[])


class TranscriptEditingTests(TestCase):
    def setUp(self):
        from ..models import TranscriptSegment
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
        from ..services.rendering import render_command
        from ..services.languages import label_cues
        from ..services.captions import write_captions
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
        from ..models import ConnectedSocialAccount, ScheduledPost
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
        from ..models import ConnectedSocialAccount
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
        from ..models import ScheduledPost
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
        from ..models import ScheduledPost
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
        from ..models import ScheduledPost
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
        from ..models import ConnectedSocialAccount, ScheduledPost
        from ..tasks import publish_youtube_post
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
        from ..models import ConnectedSocialAccount, ScheduledPost
        from ..tasks import publish_youtube_post
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
