"""Narration: TTS adapters (mocked HTTP), policy, chunking, speed, preview cache, manual upload, alignment. No network."""
import base64
import os
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from ..models import MediaAsset, ProviderUsage
from ..providers import ProviderPolicyBlocked, ProviderUnavailable
from ..providers.tts_common import chunk_text
from ..services import narration as svc
from ..services.media import StudioError, run
from ..story import alignment

CLEAN = {name: '' for name in (
    'GEMINI_API_KEY', 'GEMINI_TTS_MODEL', 'GEMINI_ENABLED', 'GROQ_API_KEY', 'GROQ_TTS_MODEL', 'GROQ_ENABLED',
    'ELEVENLABS_API_KEY', 'ELEVENLABS_PLAN_COMMERCIAL', 'ELEVENLABS_VOICES', 'AI_ALLOW_PAID_PROVIDERS',
    'AI_ALLOW_BILLABLE', 'AI_ALLOW_FREE_TIER', 'AI_DAILY_BUDGET_USD', 'AI_FREE_TIER_DAILY_CALLS',
    'AI_DAILY_PAID_CALLS', 'AI_MAX_CALLS_PER_JOB', 'AI_PROVIDER_ORDER_TTS', 'NARRATION_MAX_SECONDS')}
GEMINI = dict(CLEAN, GEMINI_API_KEY='k', GEMINI_TTS_MODEL='tts-model')
GROQ = dict(CLEAN, GROQ_API_KEY='k', GROQ_TTS_MODEL='canopylabs/orpheus-v1-english')
ELEVEN = dict(CLEAN, ELEVENLABS_API_KEY='k')
PAID_OK = dict(ELEVEN, AI_ALLOW_PAID_PROVIDERS='true', AI_DAILY_BUDGET_USD='5')
VOICE_ID = 'abcdefghij1234'


def sine(path, seconds, fmt=None):
    args = ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}']
    if fmt == 'pcm':
        args += ['-f', 's16le', '-ar', '24000', '-ac', '1']
    else:
        args += ['-ar', '24000', '-ac', '1']
    run(args + [str(path)])
    return Path(path).read_bytes()


def reply(status=200, body=None, content=b'', headers=None):
    mocked = Mock(status_code=status, headers=headers or {}, content=content)
    mocked.json.return_value = body if body is not None else {}
    return mocked


class Base(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.media = Path(tempfile.mkdtemp())
        cls.pcm2 = sine(cls.media/'two.pcm', 2, 'pcm')
        cls.pcm3 = sine(cls.media/'three.pcm', 3, 'pcm')
        cls.wav1 = sine(cls.media/'one.wav', 1)
        cls.wav4 = sine(cls.media/'four.wav', 4)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.override = override_settings(MEDIA_ROOT=Path(self.tmp.name))
        self.override.enable()
        whisper = patch.object(alignment, '_whisper_words', return_value=None)
        whisper.start()
        self.addCleanup(whisper.stop)
        self.client.get('/api/v1/session')

    def tearDown(self):
        self.override.disable()
        self.tmp.cleanup()

    def env(self, values):
        patcher = patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gemini_ok(self, pcm=None):
        body = {'steps': [{'type': 'model_output', 'content': [
            {'type': 'audio', 'data': base64.b64encode(pcm or self.pcm2).decode()}]}]}
        return patch('api.providers.tts_common.httpx.post', return_value=reply(body=body))


class VoicesTests(Base):
    def test_nothing_configured(self):
        self.env(CLEAN)
        data = self.client.get('/api/v1/narration/voices').json()
        self.assertTrue(data['manual_upload'])
        names = {p['name']: p for p in data['providers']}
        self.assertEqual(set(names), {'gemini_tts', 'groq_tts', 'elevenlabs'})
        for p in names.values():
            self.assertFalse(p['available'])
            self.assertTrue(p['reason'])
            self.assertEqual(p['voices'], [])
        self.assertIn('GEMINI_API_KEY', names['gemini_tts']['reason'])

    def test_configured_gemini_lists_voices_and_hindi_filter_hides_english_only(self):
        self.env(dict(GEMINI, GROQ_API_KEY='k', GROQ_TTS_MODEL='m'))
        data = self.client.get('/api/v1/narration/voices?language=hi').json()
        names = {p['name']: p for p in data['providers']}
        self.assertTrue(names['gemini_tts']['available'])
        self.assertEqual(len(names['gemini_tts']['voices']), 30)
        self.assertFalse(names['groq_tts']['available'])
        self.assertIn('does not support', names['groq_tts']['reason'])
        self.assertEqual(names['gemini_tts']['billing'], 'free_tier')
        self.assertIn('human reviewers', names['gemini_tts']['notes'])

    def test_paid_provider_reports_budget_reason(self):
        self.env(ELEVEN)
        names = {p['name']: p for p in self.client.get('/api/v1/narration/voices').json()['providers']}
        self.assertFalse(names['elevenlabs']['available'])
        self.assertIn('AI_ALLOW_PAID_PROVIDERS', names['elevenlabs']['reason'])


class SynthesisTests(Base):
    def test_gemini_happy_path(self):
        self.env(GEMINI)
        with self.gemini_ok() as post:
            asset = svc.synthesize_narration('o', 'Hello there. How are you today?', 'en', 'Kore', 1.0)
        self.assertEqual(post.call_count, 1)
        body = post.call_args.kwargs['json']
        self.assertEqual(body['model'], 'tts-model')
        self.assertEqual(body['generation_config']['speech_config'], [{'voice': 'Kore'}])
        self.assertEqual((asset.kind, asset.origin, asset.provider), ('narration', 'generated_tts', 'gemini_tts'))
        self.assertAlmostEqual(asset.duration, 2.0, delta=0.4)
        self.assertEqual(asset.license_status, 'commercial_use_permitted')
        self.assertEqual(asset.metadata['voice'], 'Kore')
        self.assertEqual(asset.metadata['timing'], 'approximate')
        self.assertTrue(asset.metadata['words'])
        self.assertIn('human reviewers', asset.metadata['provider_notes'])
        row = ProviderUsage.objects.get(provider='gemini_tts')
        self.assertEqual((row.status, row.unit_type, row.units), ('ok', 'characters', 31))

    def test_gemini_wav_response_also_decoded(self):
        self.env(GEMINI)
        with self.gemini_ok(self.wav1):
            asset = svc.synthesize_narration('o', 'One second.', 'hi', 'Puck')
        self.assertAlmostEqual(asset.duration, 1.0, delta=0.4)

    def test_rate_limit_route_and_cooldown(self):
        self.env(GEMINI)
        limited = reply(status=429, body={}, headers={'retry-after': '30'})
        with patch('api.providers.tts_common.httpx.post', return_value=limited):
            res = self.client.post('/api/v1/narration/synthesize', {'text': 'Hello there.', 'language': 'en', 'voice_id': 'Kore'},
                                   content_type='application/json')
        self.assertEqual(res.status_code, 429)
        body = res.json()
        self.assertEqual(body['error'], 'ai_rate_limited')
        self.assertTrue(25 <= body['retry_after_seconds'] <= 31)
        row = ProviderUsage.objects.get(provider='gemini_tts')
        self.assertEqual(row.status, 'rate_limited')
        self.assertIsNotNone(row.retry_after_until)
        with patch('api.providers.tts_common.httpx.post') as post:      # cooldown blocks the next call locally
            res = self.client.post('/api/v1/narration/synthesize', {'text': 'Hi.', 'language': 'en', 'voice_id': 'Kore'},
                                   content_type='application/json')
        self.assertEqual(res.status_code, 429)
        post.assert_not_called()
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_paid_blocked_by_policy(self):
        self.env(ELEVEN)
        with patch('api.providers.tts_common.httpx.post') as post:
            with self.assertRaises(ProviderPolicyBlocked) as ctx:
                svc.synthesize_narration('o', 'Hello.', 'en', VOICE_ID, provider='elevenlabs')
            self.assertEqual(ctx.exception.code, 'paid_inference_disabled')
            self.env(dict(ELEVEN, AI_ALLOW_PAID_PROVIDERS='true'))
            with self.assertRaises(ProviderPolicyBlocked) as ctx:
                svc.synthesize_narration('o', 'Hello.', 'en', VOICE_ID, provider='elevenlabs')
            self.assertIn('AI_DAILY_BUDGET_USD', ctx.exception.message)
        post.assert_not_called()

    def test_auto_selection_never_uses_paid(self):
        self.env(PAID_OK)
        with patch('api.providers.tts_common.httpx.post') as post:
            with self.assertRaises(ProviderUnavailable) as ctx:
                svc.synthesize_narration('o', 'Hello.', 'en', VOICE_ID)
        self.assertEqual(ctx.exception.code, 'tts_unavailable')
        self.assertIn('upload', ctx.exception.message)
        post.assert_not_called()

    def test_paid_opted_in_licence_follows_plan(self):
        self.env(PAID_OK)
        with patch('api.providers.tts_common.httpx.post', return_value=reply(content=self.pcm2)) as post:
            asset = svc.synthesize_narration('o', 'Hello there.', 'en', VOICE_ID, provider='elevenlabs')
        self.assertEqual(post.call_args.kwargs['params'], {'output_format': 'pcm_24000'})
        self.assertEqual(asset.license_status, 'restricted')
        self.assertIn('commercial licence', asset.rights_notes)
        self.assertGreater(ProviderUsage.objects.get(provider='elevenlabs').cost_usd_estimate, 0)
        self.env(dict(PAID_OK, ELEVENLABS_PLAN_COMMERCIAL='true'))
        with patch('api.providers.tts_common.httpx.post', return_value=reply(content=self.pcm3)):
            asset = svc.synthesize_narration('o', 'A different line.', 'en', VOICE_ID, provider='elevenlabs')
        self.assertEqual(asset.license_status, 'commercial_use_permitted')

    def test_english_only_provider_refuses_hindi(self):
        self.env(GROQ)
        with patch('api.providers.tts_common.httpx.post') as post:
            for language in ('hi', 'hinglish'):
                with self.assertRaises(StudioError) as ctx:
                    svc.synthesize_narration('o', 'Namaste.', language, 'austin', provider='groq_tts')
                self.assertEqual(ctx.exception.code, 'language_unsupported')
            with self.assertRaises(ProviderUnavailable):          # auto selection: nothing multilingual configured
                svc.synthesize_narration('o', 'Namaste.', 'hi', 'austin')
        post.assert_not_called()

    def test_chunking_and_concatenation(self):
        sentences = ['This is the first sentence of the narration and it is fairly long for a test, with enough extra words to be wide.',
                     'Here comes the second sentence which is also written to be reasonably long indeed, padded with a few more words.',
                     'Finally the third sentence closes the script with another sensible amount of words, plus a short extra tail.']
        chunks = chunk_text(' '.join(sentences), 200)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertEqual(' '.join(chunks), ' '.join(sentences))
        self.assertTrue(all(len(c) <= 50 for c in chunk_text('word ' * 80, 50)))
        self.env(GROQ)
        with patch('api.providers.tts_common.httpx.post', return_value=reply(content=self.wav1)) as post:
            asset = svc.synthesize_narration('o', ' '.join(sentences), 'en', 'austin', provider='groq_tts')
        self.assertEqual(post.call_count, 3)
        self.assertEqual(post.call_args.kwargs['json']['response_format'], 'wav')
        self.assertAlmostEqual(asset.duration, 3 + 2 * 0.18, delta=0.4)
        self.assertEqual(ProviderUsage.objects.filter(provider='groq_tts').count(), 3)
        self.assertEqual(asset.license_status, 'unknown')

    def test_speed_changes_duration(self):
        self.env(GEMINI)
        with self.gemini_ok(self.wav4):
            normal = svc.synthesize_narration('o', 'A steady line.', 'en', 'Kore', 1.0)
        with self.gemini_ok(self.wav4):
            fast = svc.synthesize_narration('o', 'A steady line.', 'en', 'Kore', 1.25)
        self.assertAlmostEqual(normal.duration, 4.0, delta=0.4)
        self.assertAlmostEqual(fast.duration, 3.2, delta=0.4)
        with self.assertRaises(StudioError):
            svc.synthesize_narration('o', 'x.', 'en', 'Kore', 1.5)

    def test_validation_and_cloning_rejected(self):
        self.env(GEMINI)
        with self.assertRaises(StudioError) as ctx:
            svc.synthesize_narration('o', 'x' * 2501, 'en', 'Kore')
        self.assertEqual(ctx.exception.code, 'text_too_long')
        res = self.client.post('/api/v1/narration/synthesize',
                               {'text': 'Hi.', 'language': 'en', 'voice_id': 'Kore', 'clone_voice_of': 'someone'},
                               content_type='application/json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['error'], 'voice_cloning_unsupported')

    def test_preview_cached_with_retention(self):
        self.env(GEMINI)
        with self.gemini_ok() as post:
            first = svc.preview_voice('o', 'en', 'Kore', 1.0, 'Short sample text.')
            second = svc.preview_voice('o', 'en', 'Kore', 1.0, 'Short sample text.')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(post.call_count, 1)
        delta = first.retention_until - timezone.now()
        self.assertTrue(timedelta(hours=23) < delta <= timedelta(days=1))
        self.assertTrue(first.metadata['preview'])
        with self.assertRaises(StudioError):
            svc.preview_voice('o', 'en', 'Kore', 1.0, 'x' * 241)


class UploadTests(Base):
    def upload(self, data=None, name='voice.wav', **fields):
        payload = {'file': SimpleUploadedFile(name, data if data is not None else self.wav1), **fields}
        return self.client.post('/api/v1/narration/upload', payload)

    def test_declaration_sets_owned(self):
        res = self.upload(i_recorded_this='true', title='My take')
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual((body['kind'], body['origin'], body['license_status']), ('narration', 'manual_narration', 'owned'))
        self.assertAlmostEqual(body['duration'], 1.0, delta=0.2)

    def test_no_declaration_is_unknown_and_owned_needs_declaration(self):
        self.assertEqual(self.upload().json()['license_status'], 'unknown')
        res = self.upload(self.wav4, license_status='owned')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['error'], 'declaration_required')

    def test_rejects_non_audio_and_leaves_no_files(self):
        res = self.upload(b'not audio at all', name='x.wav')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(MediaAsset.objects.count(), 0)

    def test_duration_bounds(self):
        self.env(dict(CLEAN, NARRATION_MAX_SECONDS='3'))
        res = self.upload(self.wav4)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()['error'], 'narration_duration')

    def test_attach_manual_narration_validation(self):
        asset = MediaAsset.objects.get(pk=self.upload(i_recorded_this='true').json()['id'])
        self.assertEqual(svc.attach_manual_narration(asset.owner, asset).pk, asset.pk)
        with self.assertRaises(StudioError):
            svc.attach_manual_narration('someone-else', asset)
        asset.kind = 'music'
        with self.assertRaises(StudioError):
            svc.attach_manual_narration(asset.owner, asset)


class AlignmentTests(SimpleTestCase):
    TEXT = 'Hello there, friend. This is a test! Does it work? Yes.'

    def check_monotonic(self, words, duration):
        last = 0.0
        for w in words:
            self.assertGreaterEqual(w['start'], last)
            self.assertGreaterEqual(w['end'], w['start'])
            self.assertLessEqual(w['end'], duration + 1e-6)
            last = w['start']

    def test_approximate_fallback(self):
        with patch.object(alignment, '_whisper_words', return_value=None):
            result = alignment.align_words(self.TEXT, None, 7.5)
        self.assertEqual(result['timing'], 'approximate')
        self.assertEqual([w['text'] for w in result['words']], self.TEXT.split())
        self.check_monotonic(result['words'], 7.5)
        self.assertAlmostEqual(result['words'][-1]['end'], 7.5, places=2)

    def test_provider_words_win(self):
        provided = [{'start': 0.0, 'end': 0.4, 'text': 'Hi'}, {'start': 0.5, 'end': 1.2, 'text': 'there'}]
        result = alignment.align_words('Hi there', None, 1.0, provided)
        self.assertEqual(result['timing'], 'provider')
        self.check_monotonic(result['words'], 1.0)
        self.assertEqual(result['words'][-1]['end'], 1.0)      # clamped to the audio duration

    def test_whisper_used_when_available(self):
        heard = [{'start': 0.1, 'end': 0.5, 'text': 'hello'}, {'start': 0.6, 'end': 1.0, 'text': 'world'}]
        with patch.object(alignment, '_whisper_words', return_value=heard):
            result = alignment.align_words('Hello, world!', '/x.wav', 1.2)
        self.assertEqual(result['timing'], 'local_whisper')
        self.assertEqual([w['text'] for w in result['words']], ['Hello,', 'world!'])
