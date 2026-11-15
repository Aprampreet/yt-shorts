import io
import json
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.utils import timezone
from PIL import Image
from ..models import MediaAsset, GenerationProject, GeneratedScript, StoryboardScene
from ..services import assets as svc
from ..services.media import run, StudioError


def make_video(path, seconds=1, audio=False):
    args = ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'testsrc=size=160x120:rate=10:duration={seconds}']
    if audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-shortest']
    run(args + ['-pix_fmt', 'yuv420p', str(path)])


def make_audio(path, seconds=1):
    run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', str(path)])


def make_image(path, color='red'):
    Image.new('RGB', (64, 48), color).save(path, 'PNG')


class AssetTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media = Path(tempfile.mkdtemp())
        make_video(cls.media/'clip.mp4', 1, audio=True)
        make_audio(cls.media/'tone.wav')
        make_image(cls.media/'pic.png')
        make_image(cls.media/'pic2.png', 'blue')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.override.enable()
        self.client.get('/api/v1/session')

    def tearDown(self):
        self.override.disable()
        self.tmp.cleanup()

    def upload(self, name='clip.mp4', kind='video', client=None, as_name=None, **extra):
        data = {'kind': kind, 'license_status': 'owned', **extra}
        payload = (self.media/name).read_bytes() if isinstance(name, str) else name
        data['file'] = SimpleUploadedFile(as_name or name, payload)
        return (client or self.client).post('/api/v1/assets', data)

    def owner(self):
        return self.client.session['studio_owner']

    def test_upload_video_audio_image(self):
        video = self.upload('clip.mp4', 'video', title='Clip', tags='B-Roll, city')
        self.assertEqual(video.status_code, 201, video.content)
        body = video.json()
        self.assertEqual(body['license_status'], 'owned')
        self.assertFalse(body['review_required'])
        self.assertEqual((body['width'], body['height']), (160, 120))
        self.assertGreater(body['duration'], 0.5)
        self.assertEqual(body['tags'], ['b-roll', 'city'])
        self.assertEqual(body['usage'], {'scenes': 0, 'projects': 0, 'narrations': 0})
        audio = self.upload('tone.wav', 'narration')
        self.assertEqual(audio.status_code, 201, audio.content)
        self.assertIsNone(audio.json()['width'])
        self.assertIsNone(audio.json()['thumbnail_url'])
        image = self.upload('pic.png', 'image')
        self.assertEqual(image.status_code, 201, image.content)
        self.assertEqual(image.json()['mime_type'], 'image/png')
        stored = MediaAsset.objects.get(pk=body['id'])
        self.assertTrue(stored.file.startswith(f'assets/{self.owner()}/'))
        self.assertTrue((Path(self.tmp.name)/stored.file).is_file())

    def test_kind_must_match_content_and_fake_extension_rejected(self):
        fake = self.upload(b'this is not a video at all', 'video', as_name='evil.mp4')
        self.assertEqual(fake.status_code, 400)
        self.assertEqual(self.upload(b'<html>', 'image', as_name='x.png').status_code, 400)
        self.assertEqual(self.upload('pic.png', 'video', as_name='pic.mp4').status_code, 400)
        self.assertEqual(self.upload('tone.wav', 'video').status_code, 400)
        self.assertEqual(self.upload('clip.mp4', 'subtitle').status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)
        self.assertEqual([p for p in (Path(self.tmp.name)/'assets'/self.owner()).iterdir()], [])
        # a real image under a fake video-ish name is stored with the verified extension
        ok = self.upload('pic.png', 'image', as_name='photo.exe')
        self.assertEqual(ok.status_code, 201)
        self.assertTrue(MediaAsset.objects.get(pk=ok.json()['id']).file.endswith('.png'))

    def test_license_is_required_and_attribution_rule(self):
        data = {'kind': 'image', 'file': SimpleUploadedFile('a.png', (self.media/'pic.png').read_bytes())}
        self.assertEqual(self.client.post('/api/v1/assets', data).status_code, 422)
        bad = self.upload('pic.png', 'image', license_status='maybe')
        self.assertEqual(bad.status_code, 400)
        missing = self.upload('pic.png', 'image', license_status='attribution_required')
        self.assertEqual(missing.status_code, 400)
        self.assertIn('Attribution', missing.json()['message'])
        ok = self.upload('pic.png', 'image', license_status='attribution_required', attribution_text='Photo by A')
        self.assertEqual(ok.status_code, 201)
        asset_id = ok.json()['id']
        patch = lambda body: self.client.patch(f'/api/v1/assets/{asset_id}', json.dumps(body), content_type='application/json')
        self.assertEqual(patch({'attribution_text': ''}).status_code, 400)
        self.assertEqual(patch({'license_status': 'bogus'}).status_code, 400)
        changed = patch({'license_status': 'unknown'})
        self.assertEqual(changed.status_code, 200)
        self.assertTrue(changed.json()['review_required'])
        self.assertEqual(changed.json()['license_label'], 'Unknown')
        self.assertEqual(patch({'license_status': 'attribution_required', 'attribution_text': ''}).status_code, 400)
        pinned = patch({'pinned': True, 'title': 'New', 'tags': ['X'], 'retention_until': '2030-01-01T00:00:00Z'})
        self.assertEqual(pinned.status_code, 200, pinned.content)
        self.assertTrue(pinned.json()['pinned'])
        self.assertEqual(pinned.json()['tags'], ['x'])

    def test_default_rights_not_optimistic_for_generated(self):
        folder = Path(self.tmp.name)/'gen'; folder.mkdir()
        make_audio(folder/'n.wav')
        asset = svc.register_generated_asset(self.owner(), folder/'n.wav', 'narration', 'generated_tts', 'unknown', provider='local')
        self.assertEqual(asset.license_status, 'unknown')
        self.assertEqual(svc.asset_path(asset), (folder/'n.wav').resolve())
        again = svc.register_generated_asset(self.owner(), folder/'n.wav', 'narration', 'generated_tts', 'unknown')
        self.assertEqual(again.pk, asset.pk)
        with self.assertRaises(StudioError):
            svc.register_generated_asset(self.owner(), self.media/'tone.wav', 'narration', 'generated_tts', 'owned')
        asset.file = '../escape.wav'
        with self.assertRaises(StudioError):
            svc.asset_path(asset)

    def test_dedupe_returns_existing(self):
        first = self.upload('pic.png', 'image')
        second = self.upload('pic.png', 'image', title='again')
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['deduplicated'])
        self.assertEqual(second.json()['id'], first.json()['id'])
        self.assertEqual(MediaAsset.objects.count(), 1)
        self.assertEqual(len(list((Path(self.tmp.name)/'assets'/self.owner()).iterdir())), 1)
        other = Client(); other.get('/api/v1/session')
        self.assertEqual(self.upload('pic.png', 'image', client=other).status_code, 201)

    def test_owner_isolation(self):
        asset_id = self.upload('pic.png', 'image').json()['id']
        stranger = Client(); stranger.get('/api/v1/session')
        for method, url in [('get', ''), ('get', '/file'), ('get', '/thumbnail'), ('delete', ''), ('patch', '')]:
            extra = {'data': '{}', 'content_type': 'application/json'} if method == 'patch' else {}
            response = getattr(stranger, method)(f'/api/v1/assets/{asset_id}{url}', **extra)
            self.assertEqual(response.status_code, 404, (method, url))
        self.assertEqual(stranger.get('/api/v1/assets').json()['total'], 0)

    def test_csrf_enforced(self):
        client = Client(enforce_csrf_checks=True)
        token = client.get('/api/v1/session').json()['csrf_token']
        def post(**headers):
            return client.post('/api/v1/assets', {'kind': 'image', 'license_status': 'owned',
                'file': SimpleUploadedFile('a.png', (self.media/'pic.png').read_bytes())}, **headers)
        self.assertEqual(post().status_code, 403)
        self.assertEqual(post(HTTP_X_CSRFTOKEN=token).status_code, 201)

    def test_list_filters_search_pagination(self):
        self.upload('pic.png', 'image', title='Sunset beach', tags='nature,sky', license_status='commercial_use_permitted')
        self.upload('pic2.png', 'image', title='Office', tags='work', license_status='attribution_required', attribution_text='by Zed')
        self.upload('tone.wav', 'music', title='Calm loop', tags='nature')
        listing = lambda q: self.client.get('/api/v1/assets' + q).json()
        self.assertEqual(listing('')['total'], 3)
        self.assertEqual(listing('?kind=image')['total'], 2)
        self.assertEqual(listing('?license_status=attribution_required')['items'][0]['title'], 'Office')
        self.assertEqual(listing('?tag=nature')['total'], 2)
        self.assertEqual(listing('?tag=nat')['total'], 0)
        self.assertEqual(listing('?q=sunset')['total'], 1)
        self.assertEqual(listing('?q=zed')['total'], 1)
        self.assertEqual(listing('?q=work')['items'][0]['title'], 'Office')
        page = listing('?limit=2&offset=2')
        self.assertEqual((page['total'], len(page['items'])), (3, 1))

    def test_range_preview_and_thumbnail(self):
        video = self.upload('clip.mp4', 'video').json()
        size = video['size_bytes']
        part = self.client.get(video['file_url'], HTTP_RANGE='bytes=0-9')
        self.assertEqual(part.status_code, 206)
        self.assertEqual(len(b''.join(part.streaming_content)), 10)
        self.assertEqual(part['Content-Range'], f'bytes 0-9/{size}')
        thumb = self.client.get(video['thumbnail_url'])
        self.assertEqual(thumb.status_code, 200)
        Image.open(io.BytesIO(b''.join(thumb.streaming_content))).verify()
        image = self.upload('pic.png', 'image').json()
        self.assertEqual(self.client.get(image['thumbnail_url']).status_code, 200)
        audio = self.upload('tone.wav', 'audio').json()
        self.assertEqual(self.client.get(f"/api/v1/assets/{audio['id']}/thumbnail").status_code, 404)

    def make_scene(self, asset):
        project = GenerationProject.objects.create(owner=self.owner(), kind='story', title='My Story')
        script = GeneratedScript.objects.create(project=project, version=1)
        return StoryboardScene.objects.create(script=script, order=0, start=0, end=2, visual_type='asset_image', asset=asset), project

    def test_soft_delete_conflict_and_hard_delete(self):
        first = self.upload('pic.png', 'image').json()
        used = self.upload('pic2.png', 'image').json()
        scene, project = self.make_scene(MediaAsset.objects.get(pk=used['id']))
        self.assertEqual(self.client.get(f"/api/v1/assets/{used['id']}").json()['usage'], {'scenes': 1, 'projects': 1, 'narrations': 0})
        conflict = self.client.delete(f"/api/v1/assets/{used['id']}")
        self.assertEqual(conflict.status_code, 409)
        self.assertIn('My Story', conflict.json()['message'])
        self.assertEqual(self.client.delete(f"/api/v1/assets/{used['id']}?hard=true").status_code, 409)
        soft = self.client.delete(f"/api/v1/assets/{first['id']}")
        self.assertEqual(soft.status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/assets/{first['id']}").status_code, 404)
        self.assertEqual(self.client.get('/api/v1/assets').json()['total'], 1)
        self.assertTrue(MediaAsset.objects.filter(pk=first['id'], deleted_at__isnull=False).exists())
        stored = Path(self.tmp.name)/MediaAsset.objects.get(pk=first['id']).file
        self.assertTrue(stored.exists())
        hard = self.client.delete(f"/api/v1/assets/{first['id']}?hard=true")
        self.assertEqual(hard.status_code, 200)
        self.assertFalse(stored.exists())
        self.assertFalse(MediaAsset.objects.filter(pk=first['id']).exists())
        # re-upload after soft delete creates a fresh live asset
        again = self.upload('pic.png', 'image')
        self.assertEqual(again.status_code, 201)

    def test_cleanup_respects_pinned_and_referenced(self):
        ids = {}
        for key, name in [('expired', 'pic.png'), ('pinned', 'pic2.png')]:
            ids[key] = self.upload(name, 'image').json()['id']
        ids['ref'] = self.upload('tone.wav', 'audio').json()['id']
        ids['deleted'] = self.upload('clip.mp4', 'video').json()['id']
        past = timezone.now()-timedelta(days=1)
        MediaAsset.objects.filter(pk__in=[ids['expired'], ids['pinned'], ids['ref']]).update(retention_until=past)
        MediaAsset.objects.filter(pk=ids['pinned']).update(pinned=True)
        self.make_scene(MediaAsset.objects.get(pk=ids['ref']))
        MediaAsset.objects.filter(pk=ids['deleted']).update(deleted_at=timezone.now()-timedelta(days=8))
        orphan = GenerationProject.objects.create(owner=self.owner(), kind='shorts', title='orphan')
        young = GenerationProject.objects.create(owner=self.owner(), kind='shorts', title='young')
        GenerationProject.objects.filter(pk=orphan.pk).update(created_at=timezone.now()-timedelta(days=2))
        deleted_file = Path(self.tmp.name)/MediaAsset.objects.get(pk=ids['deleted']).file
        call_command('cleanup_studio', stdout=io.StringIO())
        live = set(map(str, MediaAsset.objects.values_list('pk', flat=True)))
        self.assertEqual(live, {ids['pinned'], ids['ref']})
        self.assertFalse(deleted_file.exists())
        self.assertFalse(GenerationProject.objects.filter(pk=orphan.pk).exists())
        self.assertTrue(GenerationProject.objects.filter(pk=young.pk).exists())

    def test_oversized_rejected_before_parsing(self):
        with override_settings(MAX_ASSET_BYTES=1024):
            big = SimpleUploadedFile('big.png', b'0' * (2 * 1024 * 1024 + 10))
            response = self.client.post('/api/v1/assets', {'kind': 'image', 'license_status': 'owned', 'file': big})
            self.assertEqual(response.status_code, 413)
            self.assertEqual(MediaAsset.objects.count(), 0)
            mid = SimpleUploadedFile('mid.png', b'0' * 4096)
            self.assertEqual(self.client.post('/api/v1/assets', {'kind': 'image', 'license_status': 'owned', 'file': mid}).status_code, 413)

    def test_asset_count_cap(self):
        with override_settings(MAX_ASSETS_PER_OWNER=1):
            self.assertEqual(self.upload('pic.png', 'image').status_code, 201)
            response = self.upload('pic2.png', 'image')
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['error'], 'asset_limit')
