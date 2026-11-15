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


class RequestBoundaryTests(TestCase):
    def test_oversized_upload_is_rejected_before_parsing(self):
        with override_settings(MAX_SOURCE_BYTES=10):
            response = self.client.generic('POST','/api/v1/uploads',b'',CONTENT_LENGTH=str(2*1024*1024),content_type='multipart/form-data; boundary=test')
        self.assertEqual(response.status_code,413)
        self.assertEqual(response.json()['error'],'upload_limit')

    @patch('api.routes.jobs.disk_check',side_effect=RuntimeError('private filesystem detail'))
    def test_internal_api_errors_do_not_leak_tracebacks(self, check):
        self.client.get('/api/v1/session')
        response = self.client.post('/api/v1/jobs',json.dumps({'url':'https://youtu.be/abcdefghijk','rights_declaration':True}),content_type='application/json')
        self.assertEqual(response.status_code,500)
        self.assertEqual(response.json()['error'],'internal_error')
        self.assertNotIn('private filesystem detail',response.content.decode())


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
        from ..services.media import download_sync as download
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
        from ..services.media import download_sync
        ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception('Sign in to confirm your age')
        with self.assertRaises(StudioError) as ctx:
            download_sync('https://youtu.be/abcdefghijk', Path(self.tmp.name)/'source')
        self.assertEqual(ctx.exception.code, 'source_access_restricted')

    @patch('yt_dlp.YoutubeDL')
    def test_download_reports_connection_failure(self, ydl):
        from ..services.media import download_sync
        ydl.return_value.__enter__.return_value.extract_info.side_effect = Exception('Unable to download API page: timed out')
        with self.assertRaises(StudioError) as ctx:
            download_sync('https://youtu.be/abcdefghijk', Path(self.tmp.name)/'source')
        self.assertEqual(ctx.exception.code, 'source_connection_failed')
