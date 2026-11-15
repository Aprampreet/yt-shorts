import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
import httpx
from cryptography.fernet import Fernet
from django.test import TestCase, override_settings
from django.utils import timezone
from ..models import (ConnectedSocialAccount, GeneratedScript, GeneratedShort, GenerationJob, GenerationProject,
                      MediaAsset, ScheduledPost, SourceVideo, StoryboardScene)
from ..publishing import metadata as pubmeta
from ..publishing.tasks import publish_due_instagram_posts, publish_instagram_post
from ..services.social import seal

KEY = Fernet.generate_key().decode()
IG_ENV = {'SOCIAL_ENCRYPTION_KEY': KEY, 'INSTAGRAM_APP_ID': 'app', 'INSTAGRAM_APP_SECRET': 'secret',
          'INSTAGRAM_REDIRECT_URI': 'http://localhost:8000/api/v1/social/instagram/callback',
          'PUBLIC_MEDIA_BASE_URL': 'https://cdn.example.test/public_reels', 'INSTAGRAM_POLL_INTERVAL': '0'}


def reply(status, body):
    return httpx.Response(status, json=body, request=httpx.Request('GET', 'https://graph.instagram.com/x'))


class FakeGraph:
    """Dispatches mocked httpx.request calls like the Instagram Graph API."""
    def __init__(self, statuses=('FINISHED',), quota=(1, 100), permalink='https://www.instagram.com/reel/abc/'):
        self.statuses, self.quota, self.permalink, self.calls = list(statuses), quota, permalink, []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith('/content_publishing_limit'):
            return reply(200, {'data': [{'quota_usage': self.quota[0], 'config': {'quota_total': self.quota[1]}}]})
        if url.endswith('/media_publish'):
            return reply(200, {'id': 'MEDIA1'})
        if url.endswith('/media') and method == 'POST':
            return reply(200, {'id': 'CONTAINER1'})
        if url.endswith('/CONTAINER1'):
            code = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return reply(200, {'status_code': code, 'status': 'Video format is not supported' if code == 'ERROR' else ''})
        if url.endswith('/MEDIA1'):
            return reply(200, {'permalink': self.permalink})
        return reply(404, {'error': {'message': 'unexpected ' + url}})


class InstagramBase(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')
        self.owner = self.client.session['studio_owner']
        self.tmp = tempfile.TemporaryDirectory()
        self.media = override_settings(MEDIA_ROOT=Path(self.tmp.name)); self.media.enable()
        self.env = patch.dict(os.environ, IG_ENV); self.env.start()
        source = SourceVideo.objects.create(rights_declaration=True)
        self.job = GenerationJob.objects.create(source=source, owner=self.owner, status='needs_review')
        (Path(self.tmp.name) / 'v.mp4').write_bytes(b'video-bytes')
        self.short = GeneratedShort.objects.create(job=self.job, short_number=1, start_time=0, end_time=20, duration=20,
            title='Owned clip', description='A grounded description.', hashtags=['#Shorts', '#Clip'],
            video_file='v.mp4', transcript='The speaker explains the mission route in detail. It is a long story.')
        self.account = ConnectedSocialAccount.objects.create(owner=self.owner, platform='instagram', platform_account_id='17841',
            account_name='ig_user', encrypted_credentials=seal({'access_token': 'ig-token'}),
            token_expiry=timezone.now() + timedelta(days=30))

    def tearDown(self):
        self.env.stop(); self.media.disable(); self.tmp.cleanup()

    def post(self, **extra):
        values = dict(owner=self.owner, generated_short=self.short, social_account=self.account, platform='instagram',
                      title='Owned clip', description='A grounded description.', status='queued',
                      approval_basis='source_declaration', approved_at=timezone.now(),
                      platform_metadata={'instagram': {'caption': 'Hello reel #clip', 'hashtags': ['#clip'], 'share_to_feed': True}})
        values.update(extra)
        return ScheduledPost.objects.create(**values)

    def story(self, license_status='owned', attribution=''):
        project = GenerationProject.objects.create(owner=self.owner, kind='story')
        job = GenerationJob.objects.create(owner=self.owner, project=project, pipeline='story', status='completed')
        script = GeneratedScript.objects.create(project=project, full_text='A story about bridges. They connect places.', status='approved')
        asset = MediaAsset.objects.create(owner=self.owner, kind='image', file='a.png', title='Bridge photo',
                                          license_status=license_status, attribution_text=attribution)
        StoryboardScene.objects.create(script=script, order=0, start=0, end=3, visual_type='asset_image', asset=asset)
        return GeneratedShort.objects.create(job=job, short_number=1, start_time=0, end_time=0, duration=30, kind='story',
            script=script, title='Bridges', description='About bridges.', video_file='v.mp4',
            rights_review_status='rights_documented')


class AvailabilityTests(TestCase):
    def setUp(self):
        self.client.get('/api/v1/session')
        self.owner = self.client.session['studio_owner']
        job = GenerationJob.objects.create(source=SourceVideo.objects.create(rights_declaration=True), owner=self.owner, status='needs_review')
        self.short = GeneratedShort.objects.create(job=job, short_number=1, start_time=0, end_time=20, duration=20, title='Clip', video_file='v.mp4')

    def test_unconfigured_instagram_is_rejected_and_reported(self):
        with patch.dict(os.environ, {'INSTAGRAM_APP_ID': '', 'INSTAGRAM_APP_SECRET': '', 'INSTAGRAM_REDIRECT_URI': '', 'SOCIAL_ENCRYPTION_KEY': ''}):
            response = self.client.post('/api/v1/social/posts', json.dumps({'short_id': str(self.short.id), 'platform': 'instagram',
                'title': 't', 'description': 'd'}), content_type='application/json')
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['error'], 'instagram_unavailable')
            platforms = self.client.get('/api/v1/social/platforms').json()['instagram']
            self.assertFalse(platforms['configured'])
            self.assertFalse(platforms['can_publish'])
            self.assertIn('INSTAGRAM_APP_ID', platforms['reason'])

    def test_configured_needs_public_media_and_connection(self):
        with patch.dict(os.environ, {**IG_ENV, 'PUBLIC_MEDIA_BASE_URL': ''}):
            info = self.client.get('/api/v1/social/platforms').json()['instagram']
            self.assertTrue(info['configured']); self.assertFalse(info['can_publish'])
            self.assertIn('PUBLIC_MEDIA_BASE_URL', info['reason'])
        with patch.dict(os.environ, IG_ENV):
            info = self.client.get('/api/v1/social/platforms').json()['instagram']
            self.assertFalse(info['can_publish']); self.assertFalse(info['connected'])
            ConnectedSocialAccount.objects.create(owner=self.owner, platform='instagram', platform_account_id='1', account_name='x', encrypted_credentials='e')
            self.assertTrue(self.client.get('/api/v1/social/platforms').json()['instagram']['can_publish'])

    def test_connect_url_and_state_mismatch(self):
        with patch.dict(os.environ, IG_ENV):
            url = self.client.get('/api/v1/social/instagram/connect').json()['authorization_url']
            self.assertIn('instagram_business_content_publish', url.replace('%2C', ','))
            self.assertIn('state=', url)
            session = self.client.session
            self.assertTrue(session['instagram_oauth_state'])
            with patch('api.publishing.instagram.httpx.request') as request:
                response = self.client.get('/api/v1/social/instagram/callback?code=abc&state=wrong')
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['error'], 'oauth_rejected')
            request.assert_not_called()

    def test_callback_stores_sealed_long_lived_token(self):
        with patch.dict(os.environ, IG_ENV):
            self.client.get('/api/v1/social/instagram/connect')
            state = self.client.session['instagram_oauth_state']

            def fake(method, url, **kwargs):
                if url.endswith('/oauth/access_token'):
                    return reply(200, {'data': [{'access_token': 'short', 'user_id': 77}]})
                if url.endswith('/access_token'):
                    return reply(200, {'access_token': 'long-token', 'expires_in': 5183944})
                return reply(200, {'user_id': '77', 'username': 'creator'})
            with patch('api.publishing.instagram.httpx.request', side_effect=fake):
                response = self.client.get(f'/api/v1/social/instagram/callback?code=abc&state={state}')
            self.assertEqual(response.status_code, 302)
            account = ConnectedSocialAccount.objects.get(platform='instagram')
            self.assertEqual(account.account_name, 'creator')
            self.assertNotIn('long-token', account.encrypted_credentials)


class InstagramPublishTests(InstagramBase):
    def run_task(self, post, graph):
        with patch('api.publishing.instagram.httpx.request', side_effect=graph), patch('api.publishing.instagram.time.sleep'):
            publish_instagram_post(str(post.id))
        post.refresh_from_db()

    def test_container_poll_publish_happy_path(self):
        post = self.post()
        graph = FakeGraph(statuses=('IN_PROGRESS', 'FINISHED'))
        self.run_task(post, graph)
        self.assertEqual(post.status, 'published')
        self.assertEqual(post.external_post_id, 'MEDIA1')
        self.assertEqual(post.published_url, 'https://www.instagram.com/reel/abc/')
        self.assertIsNotNone(post.published_at)
        create = next(c for c in graph.calls if c[1].endswith('/media') and c[0] == 'POST')
        self.assertEqual(create[2]['data']['media_type'], 'REELS')
        self.assertTrue(create[2]['data']['video_url'].startswith('https://cdn.example.test/public_reels/'))
        self.assertEqual(create[2]['headers']['Authorization'], 'Bearer ig-token')
        self.assertEqual(list((Path(self.tmp.name) / 'public_reels').glob('*')), [])  # staged copy removed
        # idempotent: a duplicate delivery does nothing
        calls = len(graph.calls)
        self.run_task(post, graph)
        self.assertEqual(len(graph.calls), calls)

    def test_missing_permalink_is_not_reported_published(self):
        post = self.post()
        self.run_task(post, FakeGraph(permalink=''))
        self.assertEqual(post.status, 'failed')
        self.assertEqual(post.external_post_id, 'MEDIA1')
        self.assertIn('permalink', post.last_error)
        ScheduledPost.objects.filter(pk=post.pk).update(status='queued')
        graph = FakeGraph()
        self.run_task(post, graph)  # re-check only; must not publish a second time
        self.assertEqual(post.status, 'published')
        self.assertFalse(any(c[1].endswith('/media_publish') for c in graph.calls))

    def test_container_error_sets_failed_with_message(self):
        post = self.post()
        self.run_task(post, FakeGraph(statuses=('ERROR',)))
        self.assertEqual(post.status, 'failed')
        self.assertIn('Video format is not supported', post.last_error)
        self.assertEqual(post.external_post_id, '')

    def test_processing_timeout_sets_failed(self):
        post = self.post()
        with patch.dict(os.environ, {'INSTAGRAM_POLL_TIMEOUT': '0'}):
            self.run_task(post, FakeGraph(statuses=('IN_PROGRESS',)))
        self.assertEqual(post.status, 'failed')
        self.assertIn('still processing', post.last_error)
        self.assertEqual(post.external_post_id, '')

    def test_api_rejection_surfaces_meta_message(self):
        post = self.post()

        def graph(method, url, **kwargs):
            if url.endswith('/content_publishing_limit'):
                return reply(200, {'data': [{'quota_usage': 0, 'config': {'quota_total': 100}}]})
            return reply(400, {'error': {'message': 'Media URL is not reachable', 'code': 9004}})
        self.run_task(post, graph)
        self.assertEqual(post.status, 'failed')
        self.assertIn('Media URL is not reachable', post.last_error)

    def test_transient_error_retries_then_succeeds_state_is_queued(self):
        post = self.post()
        with patch('api.publishing.instagram.httpx.request', side_effect=httpx.ConnectError('offline')), \
             patch('api.publishing.tasks.publish_instagram_post.apply_async') as retry:
            publish_instagram_post(str(post.id))
        post.refresh_from_db()
        self.assertEqual(post.status, 'queued'); self.assertEqual(post.retry_count, 1)
        retry.assert_called_once()

    def test_quota_exhausted_fails_immediate_post_and_keeps_scheduled_post_waiting(self):
        immediate = self.post()
        self.run_task(immediate, FakeGraph(quota=(100, 100)))
        self.assertEqual(immediate.status, 'failed'); self.assertIn('limit', immediate.last_error)
        self.short2 = GeneratedShort.objects.create(job=self.job, short_number=2, start_time=0, end_time=5, duration=5, title='B', video_file='v.mp4')
        waiting = self.post(generated_short=self.short2, scheduled_at=timezone.now() - timedelta(minutes=1))
        self.run_task(waiting, FakeGraph(quota=(100, 100)))
        self.assertEqual(waiting.status, 'scheduled')

    def test_rights_and_asset_gate_apply_at_publish(self):
        self.job.source.rights_declaration = False; self.job.source.save()
        post = self.post()
        graph = FakeGraph()
        self.run_task(post, graph)
        self.assertEqual(post.status, 'failed')
        self.assertEqual(graph.calls, [])
        story = self.story(license_status='unknown')
        post2 = self.post(generated_short=story)
        self.run_task(post2, graph)
        self.assertEqual(post2.status, 'failed'); self.assertIn('Bridge photo', post2.last_error)
        self.assertEqual(graph.calls, [])

    def test_attribution_is_appended_at_publish(self):
        story = self.story(license_status='attribution_required', attribution='Photo by Ada, CC BY 4.0')
        post = self.post(generated_short=story, platform_metadata={'instagram': {'caption': 'About bridges', 'hashtags': []}})
        graph = FakeGraph()
        self.run_task(post, graph)
        self.assertEqual(post.status, 'published')
        create = next(c for c in graph.calls if c[1].endswith('/media') and c[0] == 'POST')
        self.assertIn('Photo by Ada, CC BY 4.0', create[2]['data']['caption'])


class SchedulerTests(InstagramBase):
    def test_only_approved_due_posts_publish_once(self):
        past = timezone.now() - timedelta(minutes=2)
        shorts = [GeneratedShort.objects.create(job=self.job, short_number=n, start_time=0, end_time=5, duration=5, title=str(n), video_file='v.mp4') for n in range(2, 6)]
        due = self.post(status='scheduled', scheduled_at=past)
        self.post(generated_short=shorts[0], status='scheduled', scheduled_at=timezone.now() + timedelta(hours=2))
        self.post(generated_short=shorts[1], status='draft', scheduled_at=past, approved_at=None)
        self.post(generated_short=shorts[2], status='scheduled', scheduled_at=past, approved_at=None)
        self.post(generated_short=shorts[3], status='scheduled', scheduled_at=past, platform='youtube')
        with patch('api.publishing.tasks.publish_instagram_post.delay') as delay:
            self.assertEqual(publish_due_instagram_posts(), 1)
            self.assertEqual(publish_due_instagram_posts(), 0)
        delay.assert_called_once_with(str(due.id))
        due.refresh_from_db()
        self.assertEqual(due.status, 'queued')

    def test_approving_future_post_marks_scheduled_only_when_queued_for_scheduler(self):
        post = self.post(status='draft', approved_at=None, scheduled_at=timezone.now() + timedelta(hours=3))
        with patch('api.publishing.tasks.publish_instagram_post.delay') as delay:
            response = self.client.post(f'/api/v1/social/posts/{post.id}/approve')
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()['status'], 'scheduled')
        delay.assert_not_called()
        self.assertEqual(self.client.post(f'/api/v1/social/posts/{post.id}/cancel').json()['status'], 'cancelled')

    def test_approving_immediate_post_enqueues_publish(self):
        post = self.post(status='draft', approved_at=None)
        with patch('api.publishing.tasks.publish_instagram_post.delay') as delay:
            response = self.client.post(f'/api/v1/social/posts/{post.id}/approve')
        self.assertEqual(response.json()['status'], 'queued')
        delay.assert_called_once_with(str(post.id))

    def test_approval_blocked_without_public_media_or_rights(self):
        post = self.post(status='draft', approved_at=None)
        with patch.dict(os.environ, {'PUBLIC_MEDIA_BASE_URL': ''}):
            self.assertEqual(self.client.post(f'/api/v1/social/posts/{post.id}/approve').status_code, 409)
        story = self.story(license_status='permission_pending')
        blocked = self.post(generated_short=story, status='draft', approved_at=None)
        response = self.client.post(f'/api/v1/social/posts/{blocked.id}/approve')
        self.assertEqual(response.status_code, 409)
        self.assertIn('permission_pending', response.json()['message'] if 'message' in response.json() else response.content.decode())


class MetadataTests(InstagramBase):
    def test_draft_persists_platform_metadata_and_it_is_editable(self):
        response = self.client.post('/api/v1/social/posts', json.dumps({'short_id': str(self.short.id), 'platform': 'instagram',
            'account_id': str(self.account.id), 'title': 'Owned clip', 'description': 'A grounded description.\n\nSource: https://x.test',
            'hashtags': ['#clip', '#mission']}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        meta = body['platform_metadata']['instagram']
        self.assertIn('A grounded description.', meta['caption'])
        self.assertNotIn('Source:', meta['caption'])
        self.assertTrue(meta['share_to_feed'])
        edit = self.client.put(f"/api/v1/social/posts/{body['id']}/metadata", json.dumps({'caption': 'My caption #clip', 'share_to_feed': False,
            'hashtags': ['#clip']}), content_type='application/json')
        self.assertEqual(edit.status_code, 200, edit.content)
        saved = ScheduledPost.objects.get(pk=body['id']).platform_metadata['instagram']
        self.assertEqual(saved['caption'], 'My caption #clip'); self.assertFalse(saved['share_to_feed'])
        preview = self.client.get(f"/api/v1/social/posts/{body['id']}/metadata").json()
        self.assertEqual(preview['source'], 'saved'); self.assertEqual(preview['limits']['caption_max'], 2200)

    def test_caption_and_hashtag_limits_enforced(self):
        post = self.post(status='draft', approved_at=None)
        url = f'/api/v1/social/posts/{post.id}/metadata'
        put = lambda body: self.client.put(url, json.dumps(body), content_type='application/json')
        self.assertEqual(put({'caption': 'x' * 2201}).status_code, 422)
        self.assertEqual(put({'caption': 'ok', 'hashtags': [f'#tag{i}' for i in range(31)]}).status_code, 422)
        self.assertEqual(put({'caption': ' '.join(f'#t{i}' for i in range(31))}).status_code, 422)
        self.assertEqual(put({'caption': 'x' * 2200, 'hashtags': [f'#tag{i}' for i in range(30)]}).status_code, 200)
        self.assertEqual(put({'caption': 'ok', 'hashtags': ['bad tag']}).status_code, 422)

    def test_builder_limits_and_attribution_reserved(self):
        caption = pubmeta.build_instagram_caption('word ' * 1000, ['#a'] * 30, 'What do you think?', ['Photo by Ada'])
        self.assertLessEqual(len(caption), 2200)
        self.assertIn('Photo by Ada', caption)
        self.assertTrue(caption.endswith(' '.join(['#a'] * 30)))

    def test_attribution_included_in_built_metadata_and_cannot_be_edited_away(self):
        story = self.story(license_status='attribution_required', attribution='Music: Kevin, CC BY')
        meta, _ = pubmeta.build_metadata(story, 'instagram', title='Bridges', description='About bridges.')
        self.assertIn('Credits: Music: Kevin, CC BY', meta['caption'])
        post = self.post(generated_short=story, status='draft', approved_at=None)
        edit = self.client.put(f'/api/v1/social/posts/{post.id}/metadata', json.dumps({'caption': 'No credits here'}), content_type='application/json')
        self.assertEqual(edit.status_code, 200, edit.content)
        self.assertIn('Music: Kevin, CC BY', edit.json()['platform_metadata']['instagram']['caption'])

    def test_extractive_fallback_without_groq_and_youtube_disclosure(self):
        story = self.story()
        story.description = ''; story.title = ''; story.hashtags = []; story.save()
        meta, source = pubmeta.build_metadata(story, 'instagram')
        self.assertEqual(source, 'extractive')
        self.assertIn('A story about bridges', meta['caption'])
        yt, _ = pubmeta.build_metadata(story, 'youtube')
        self.assertLessEqual(len(yt['title']), 100)
        self.assertFalse(yt['altered_content'])
        story.script.narration_asset = MediaAsset.objects.create(owner=self.owner, kind='narration', file='n.wav', origin='generated_tts', license_status='owned')
        story.script.save()
        story = GeneratedShort.objects.get(pk=story.pk)
        self.assertTrue(pubmeta.build_metadata(story, 'youtube')[0]['altered_content'])

    def test_story_video_uses_same_post_endpoints(self):
        story = self.story()
        response = self.client.post('/api/v1/social/posts', json.dumps({'short_id': str(story.id), 'platform': 'instagram',
            'account_id': str(self.account.id), 'title': 'Bridges', 'description': 'About bridges.'}), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        with patch('api.publishing.tasks.publish_instagram_post.delay'):
            approve = self.client.post(f"/api/v1/social/posts/{response.json()['id']}/approve")
        self.assertEqual(approve.status_code, 202, approve.content)

    def test_groq_metadata_used_when_available(self):
        generated = {'title': 'Mission route explained', 'description': 'Walks the route.\n\nEnjoyed this Short? Hit like, subscribe for more, and tell me your take in the comments!\n\n#Shorts',
                     'hashtags': ['#Shorts', '#mission', '#route']}
        with patch('api.services.groq.enabled', return_value=True), patch('api.services.groq.generate_metadata', return_value=generated):
            meta, source = pubmeta.build_metadata(self.short, 'instagram', use_groq=True)
        self.assertEqual(source, 'groq')
        self.assertIn('Walks the route.', meta['caption'])
        self.assertNotIn('subscribe', meta['caption'])
