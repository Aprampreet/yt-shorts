"""Regression tests for the cleanup pass: per-pipeline capacity, story videos without a source, Instagram
capability flag, YouTube synthetic-media disclosure, Groq metadata cooldown, validation errors and retention."""
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import httpx
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from ninja.errors import HttpError

from ..models import (ConnectedSocialAccount, GeneratedShort, GenerationJob, GenerationProject, MediaAsset,
                      ProviderUsage, ScheduledPost, SourceVideo)
from ..providers import registry
from ..routes import jobs as jobs_routes
from ..routes import story as story_routes
from .test_providers import GROQ


def make_job(owner='o', pipeline='shorts', status='queued', source=True, created=None):
    job = GenerationJob.objects.create(
        source=SourceVideo.objects.create(source_type='upload') if source else None,
        owner=owner, pipeline=pipeline, status=status)
    if created:
        GenerationJob.objects.filter(pk=job.pk).update(created_at=created)
    return job


class PerPipelineCapacityTests(TestCase):
    def check_shorts(self):
        with patch('api.routes.jobs.disk_check'):
            jobs_routes.capacity('o')

    def check_story(self):
        with patch('api.routes.story.disk_check'):
            story_routes.capacity('o')

    def test_active_story_job_does_not_block_shorts(self):
        make_job(pipeline='story', status='running')
        self.check_shorts()
        with self.assertRaises(HttpError):
            self.check_story()

    def test_active_shorts_job_does_not_block_story_but_blocks_shorts(self):
        make_job(pipeline='shorts', status='running')
        self.check_story()
        with self.assertRaises(HttpError) as raised:
            self.check_shorts()
        self.assertEqual(raised.exception.status_code, 429)

    def test_story_steps_do_not_use_the_shorts_hourly_limit(self):
        for _ in range(jobs_routes.HOURLY_SHORTS_JOBS + 2):
            make_job(pipeline='story', status='completed')
        self.check_shorts()

    def test_shorts_hourly_limit_still_applies_and_ignores_old_jobs(self):
        for _ in range(jobs_routes.HOURLY_SHORTS_JOBS):
            make_job(pipeline='shorts', status='completed')
        with self.assertRaises(HttpError) as raised:
            self.check_shorts()
        self.assertIn('Hourly', raised.exception.message)
        GenerationJob.objects.update(created_at=timezone.now() - timedelta(hours=2))
        self.check_shorts()

    def test_story_hourly_limit_is_30_and_ignores_shorts(self):
        self.assertEqual(story_routes.HOURLY_STORY_JOBS, 30)
        for _ in range(8):
            make_job(pipeline='shorts', status='completed')
        self.check_story()
        for _ in range(30):
            make_job(pipeline='story', status='completed')
        with self.assertRaises(HttpError):
            self.check_story()

    def test_global_active_cap_is_shared(self):
        for index in range(jobs_routes.GLOBAL_ACTIVE_LIMIT):
            make_job(owner=f'other-{index}', pipeline='story' if index % 2 else 'shorts', status='running')
        with self.assertRaises(HttpError):
            self.check_shorts()
        with self.assertRaises(HttpError):
            self.check_story()


class StoryVideoWithoutSourceTests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')
        self.owner = self.client.session['studio_owner']
        project = GenerationProject.objects.create(owner=self.owner, kind='story')
        self.job = GenerationJob.objects.create(owner=self.owner, pipeline='story', project=project, status='completed')
        self.assertIsNone(self.job.source)
        self.short = GeneratedShort.objects.create(job=self.job, short_number=1, start_time=0, end_time=0, duration=20,
                                                   title='Story', video_file='jobs/x/final.mp4', kind='story',
                                                   project=project)
        self.account = ConnectedSocialAccount.objects.create(owner=self.owner, platform='youtube',
                                                             platform_account_id='c', account_name='c',
                                                             encrypted_credentials='x')

    def post(self, basis='source_declaration'):
        return ScheduledPost.objects.create(owner=self.owner, generated_short=self.short, social_account=self.account,
                                            platform='youtube', title='T', description='D', status='queued',
                                            approved_at=timezone.now(), approval_basis=basis)

    def test_publish_without_source_declaration_basis_fails_cleanly(self):
        from ..tasks import publish_youtube_post
        post = self.post()
        with patch('api.services.social.youtube_upload') as upload:
            publish_youtube_post(str(post.pk))
        post.refresh_from_db()
        self.assertEqual(post.status, 'failed')
        upload.assert_not_called()

    def test_publish_with_documented_rights_proceeds_past_the_rights_rule(self):
        from ..tasks import publish_youtube_post
        self.short.rights_review_status = 'rights_documented'
        self.short.save(update_fields=['rights_review_status'])
        post = self.post(basis='manual_review')
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=Path(media)):
            (Path(media) / 'jobs/x').mkdir(parents=True)
            (Path(media) / 'jobs/x/final.mp4').write_bytes(b'x')
            with patch('api.services.social.youtube_upload', return_value=('vid1', 'public')) as upload:
                publish_youtube_post(str(post.pk))
        post.refresh_from_db()
        upload.assert_called_once()
        self.assertEqual((post.status, post.external_post_id), ('published', 'vid1'))

    def test_approve_paths_do_not_crash_without_a_source(self):
        post = ScheduledPost.objects.create(owner=self.owner, generated_short=self.short, social_account=self.account,
                                            platform='youtube', title='T', description='D', status='draft',
                                            approval_basis='source_declaration')
        response = self.client.post(f'/api/v1/social/posts/{post.pk}/approve')
        self.assertEqual(response.status_code, 409)
        batch = self.client.post('/api/v1/social/approve-batch', json.dumps({'post_ids': [str(post.pk)]}),
                                 content_type='application/json')
        self.assertEqual(batch.status_code, 409)
        self.assertEqual(self.client.post(f'/api/v1/jobs/{self.job.pk}/schedule-batch').status_code, 409)


class InstagramCapabilityTests(TestCase):
    FULL = {'INSTAGRAM_APP_ID': 'a', 'INSTAGRAM_APP_SECRET': 's', 'INSTAGRAM_REDIRECT_URI': 'https://x/cb',
            'SOCIAL_ENCRYPTION_KEY': 'k', 'PUBLIC_MEDIA_BASE_URL': 'https://media.example.com/public_reels'}

    def flag(self, **env):
        values = dict({name: '' for name in self.FULL}, **env)
        with patch.dict(os.environ, values):
            return self.client.get('/api/v1/capabilities').json()['instagram_publishing_available']

    def test_false_when_unconfigured(self):
        self.assertIs(self.flag(), False)

    def test_false_without_public_media_url(self):
        self.assertIs(self.flag(**dict(self.FULL, PUBLIC_MEDIA_BASE_URL='')), False)

    def test_true_when_configured_with_public_media(self):
        self.assertIs(self.flag(**self.FULL), True)

    def test_matches_social_platforms_can_publish_once_connected(self):
        self.client.get('/api/v1/session')
        owner = self.client.session['studio_owner']
        ConnectedSocialAccount.objects.create(owner=owner, platform='instagram', platform_account_id='i',
                                              account_name='i', encrypted_credentials='x')
        with patch.dict(os.environ, self.FULL):
            platforms = self.client.get('/api/v1/social/platforms').json()['instagram']
            available = self.client.get('/api/v1/capabilities').json()['instagram_publishing_available']
        self.assertEqual(platforms['can_publish'], available)


class YoutubeSyntheticDisclosureTests(TestCase):
    def upload_body(self, metadata):
        from ..services import social
        post = Mock(platform_metadata=metadata, encrypted_upload_session='', scheduled_at=None, hashtags=['#Shorts'],
                    title='T', description='D')
        client = MagicMock()
        client.post.return_value = Mock(headers={'Location': 'https://www.googleapis.com/upload/session'})
        client.put.return_value = Mock(status_code=200, json=lambda: {'id': 'vid', 'status': {'privacyStatus': 'public'}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'v.mp4'
            path.write_bytes(b'12345')
            with patch.object(social, 'current_token', return_value='t'), patch.object(social, 'seal', return_value='s'), \
                    patch('api.services.social.httpx.Client') as factory:
                factory.return_value.__enter__.return_value = client
                social.youtube_upload(post, path)
        return client.post.call_args.kwargs['json']

    def test_flag_is_sent_when_altered_content_is_set(self):
        body = self.upload_body({'youtube': {'altered_content': True}})
        self.assertIs(body['status']['containsSyntheticMedia'], True)
        self.assertEqual(body['status']['privacyStatus'], 'public')

    def test_flag_is_absent_otherwise(self):
        self.assertNotIn('containsSyntheticMedia', self.upload_body({'youtube': {'altered_content': False}})['status'])
        self.assertNotIn('containsSyntheticMedia', self.upload_body({})['status'])


class GroqMetadataCooldownTests(TestCase):
    def setUp(self):
        self.job = make_job(owner='owner-1', source=False, pipeline='story')

    def test_429_from_generate_metadata_starts_a_cooldown(self):
        limited = httpx.Response(429, headers={'retry-after': '30'}, request=httpx.Request('POST', 'https://x.test'))
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post', return_value=limited) as post:
            self.assertIsNone(registry.get('groq', job=self.job).metadata('Words were spoken here.'))
            self.assertEqual(post.call_count, 1)
        row = ProviderUsage.objects.get()
        self.assertEqual((row.status, row.http_status), ('rate_limited', 429))
        self.assertAlmostEqual((row.retry_after_until - timezone.now()).total_seconds(), 30, delta=5)
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post') as post:
            self.assertIsNone(registry.get('groq', job=self.job).metadata('Words were spoken here.'))
            post.assert_not_called()      # blocked by the cooldown, no new request

    def test_generate_metadata_alone_still_returns_none(self):
        from ..services import groq
        limited = httpx.Response(429, request=httpx.Request('POST', 'https://x.test'))
        seen = []
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post', return_value=limited):
            self.assertIsNone(groq.generate_metadata('Words were spoken here.'))
            self.assertIsNone(groq.generate_metadata('Words were spoken here.', on_response=seen.append))
        self.assertEqual([r.status_code for r in seen], [429])


class ValidationErrorTests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')

    def test_422_keeps_detail_list_and_adds_message(self):
        response = self.client.post('/api/v1/jobs', json.dumps({'rights_declaration': True}),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertIsInstance(body['detail'], list)
        self.assertTrue(body['detail'])
        self.assertEqual(body['error'], 'validation_error')
        self.assertIn('url', body['message'])

    def test_nested_field_is_named(self):
        response = self.client.post('/api/v1/jobs', json.dumps(
            {'url': 'https://youtu.be/x', 'rights_declaration': True, 'options': {'target_duration': 5}}),
            content_type='application/json')
        self.assertEqual(response.status_code, 422)
        self.assertIn('target_duration', response.json()['message'])


class CleanupStudioTests(TestCase):
    def test_story_jobs_without_source_and_expired_previews_are_cleaned(self):
        with tempfile.TemporaryDirectory() as media, override_settings(MEDIA_ROOT=Path(media)):
            old = timezone.now() - timedelta(days=30)
            job = make_job(pipeline='story', status='completed', source=False, created=old)
            folder = Path(media) / 'jobs' / str(job.pk)
            (folder / 'renders').mkdir(parents=True)
            (folder / 'renders' / 'a.mp4').write_bytes(b'x')
            audio = Path(media) / 'assets' / 'preview.wav'
            audio.parent.mkdir(parents=True)
            audio.write_bytes(b'x')
            preview = MediaAsset.objects.create(owner='o', kind='narration', title='preview', file='assets/preview.wav',
                                                license_status='own_work', origin='generated_tts',
                                                retention_until=timezone.now() - timedelta(hours=1))
            call_command('cleanup_studio', stdout=MagicMock())
            self.assertFalse(folder.exists())
            self.assertFalse(GenerationJob.objects.filter(pk=job.pk).exists())
            self.assertFalse(MediaAsset.objects.filter(pk=preview.pk).exists())
            self.assertFalse(audio.exists())

    def test_beat_task_is_registered_and_calls_the_command(self):
        from django.conf import settings
        from .. import tasks
        self.assertEqual(settings.CELERY_BEAT_SCHEDULE['cleanup-studio']['task'], 'api.tasks.cleanup_studio_task')
        with patch('django.core.management.call_command') as command:
            tasks.cleanup_studio_task()
        command.assert_called_once_with('cleanup_studio')
