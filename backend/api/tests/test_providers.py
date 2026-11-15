"""Provider layer: policy matrix, ledger, cooldowns, adapters and the status endpoints. No network."""
import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone
from pydantic import BaseModel

from ..models import GenerationJob, ProviderUsage, SourceVideo
from ..providers import (Billing, Capability, Estimate, ProviderInfo, ProviderPolicyBlocked, ProviderRateLimited,
                         ProviderUnavailable, ledger, policy, registry)
from ..providers.groq_adapter import GroqProvider, cues_from_verbose_json
from ..services.media import StudioError

CLEAN = {name: '' for name in (
    'HF_TOKEN', 'HF_TEXT_MODEL', 'HF_TRANSCRIPTION_MODEL', 'HF_TRANSLATION_MODEL', 'GROQ_API_KEY', 'GROQ_ENABLED',
    'GEMINI_API_KEY', 'GEMINI_TEXT_MODEL', 'GEMINI_VISION_MODEL', 'GEMINI_ENABLED', 'AI_ALLOW_PAID_PROVIDERS',
    'AI_ALLOW_BILLABLE', 'AI_ALLOW_FREE_TIER', 'AI_DAILY_BUDGET_USD', 'AI_FREE_TIER_DAILY_CALLS',
    'AI_DAILY_PAID_CALLS', 'AI_MAX_CALLS_PER_JOB', 'AI_PROVIDER_ORDER_TEXT', 'GROQ_TEXT_MODEL')}
GROQ = dict(CLEAN, GROQ_API_KEY='test-key', GROQ_ENABLED='true')


def info(billing, name='fake'):
    return ProviderInfo(name=name, label=name.title(), capabilities=frozenset({Capability.TEXT}), billing=billing)


def usage(name='fake', billing=Billing.FREE_TIER, job=None, **kwargs):
    return ProviderUsage.objects.create(provider=name, capability='text', billing=str(billing), job=job, **kwargs)


def response(status=200, body=None, headers=None):
    mocked = Mock(status_code=status, headers=headers or {})
    mocked.json.return_value = body if body is not None else {}
    return mocked


def chat(content, tokens=42):
    return response(body={'choices': [{'message': {'content': json.dumps(content)}}], 'usage': {'total_tokens': tokens}})


class Answer(BaseModel):
    title: str
    score: float


class PolicyMatrixTests(TestCase):
    def setUp(self):
        self.job = GenerationJob.objects.create(source=SourceVideo.objects.create(source_type='upload'), owner='o')

    def decide(self, billing, env=None, estimate=None, job=None):
        with patch.dict(os.environ, dict(CLEAN, **(env or {}))):
            return policy.check(info(billing), estimate, job)

    def test_local_is_always_allowed(self):
        usage(billing=Billing.LOCAL, retry_after_until=timezone.now() + timedelta(hours=1))
        self.assertTrue(self.decide(Billing.LOCAL, {'AI_ALLOW_FREE_TIER': 'false'}).allowed)

    def test_free_tier_flag_cooldown_and_daily_cap(self):
        self.assertTrue(self.decide(Billing.FREE_TIER).allowed)                       # default on
        blocked = self.decide(Billing.FREE_TIER, {'AI_ALLOW_FREE_TIER': 'false'})
        self.assertEqual((blocked.allowed, blocked.code), (False, 'free_tier_disabled'))
        usage(); usage()
        self.assertEqual(self.decide(Billing.FREE_TIER, {'AI_FREE_TIER_DAILY_CALLS': '2'}).code, 'ai_budget')
        self.assertTrue(self.decide(Billing.FREE_TIER, {'AI_FREE_TIER_DAILY_CALLS': '3'}).allowed)
        usage(status='rate_limited', retry_after_until=timezone.now() + timedelta(minutes=5))
        cooling = self.decide(Billing.FREE_TIER)
        self.assertEqual(cooling.code, 'ai_rate_limited')
        self.assertIn('rate limited until', cooling.reason)

    def test_metered_needs_flag_and_respects_job_and_daily_caps(self):
        self.assertEqual(self.decide(Billing.METERED).code, 'paid_inference_disabled')
        self.assertTrue(self.decide(Billing.METERED, {'AI_ALLOW_BILLABLE': 'true'}).allowed)          # legacy name
        self.assertTrue(self.decide(Billing.METERED, {'AI_ALLOW_PAID_PROVIDERS': 'true'}).allowed)
        usage(billing=Billing.METERED, job=self.job); usage(billing=Billing.METERED, job=self.job)
        usage(billing=Billing.FREE_TIER, job=self.job)       # free-tier calls do not eat the paid per-job cap
        env = {'AI_ALLOW_PAID_PROVIDERS': 'true', 'AI_MAX_CALLS_PER_JOB': '2'}
        self.assertEqual(self.decide(Billing.METERED, env, job=self.job).code, 'ai_budget')
        self.assertTrue(self.decide(Billing.METERED, dict(env, AI_MAX_CALLS_PER_JOB='3'), job=self.job).allowed)
        self.assertEqual(self.decide(Billing.METERED, {'AI_ALLOW_PAID_PROVIDERS': 'true', 'AI_DAILY_PAID_CALLS': '2'}).code,
                         'ai_budget')

    def test_paid_needs_flag_positive_budget_estimate_and_headroom(self):
        est = Estimate(units=100, unit_type='characters', usd=Decimal('0.40'))
        self.assertEqual(self.decide(Billing.PAID, estimate=est).code, 'paid_inference_disabled')
        zero = self.decide(Billing.PAID, {'AI_ALLOW_PAID_PROVIDERS': 'true'}, est)
        self.assertEqual(zero.code, 'ai_budget')
        self.assertIn('AI_DAILY_BUDGET_USD is 0', zero.reason)
        env = {'AI_ALLOW_PAID_PROVIDERS': 'true', 'AI_DAILY_BUDGET_USD': '1.00'}
        self.assertIn('no cost estimate', self.decide(Billing.PAID, env).reason)
        self.assertTrue(self.decide(Billing.PAID, env, est).allowed)
        usage(billing=Billing.PAID, cost_usd_estimate=Decimal('0.70'))
        over = self.decide(Billing.PAID, env, est)
        self.assertEqual(over.code, 'ai_budget')
        self.assertIn('Daily AI budget reached', over.reason)
        usage(billing=Billing.PAID, cost_usd_estimate=Decimal('5'), status='blocked_by_policy')   # not counted
        self.assertFalse(self.decide(Billing.PAID, env, est).allowed)
        self.assertTrue(self.decide(Billing.PAID, dict(env, AI_DAILY_BUDGET_USD='1.10'), est).allowed)

    def test_env_only_mode_skips_ledger(self):
        with patch.dict(os.environ, dict(CLEAN, AI_ALLOW_BILLABLE='true')), \
             patch.object(ledger, 'cooldown_until', side_effect=AssertionError('no DB in env-only mode')):
            self.assertTrue(policy.check(info(Billing.METERED), use_db=False).allowed)


class RetryAfterParsingTests(SimpleTestCase):
    def test_header_forms(self):
        now = timezone.now()
        self.assertEqual(ledger.parse_retry_after({'retry-after': '30'}, now), now + timedelta(seconds=30))
        self.assertEqual(ledger.parse_retry_after({'Retry-After': '30'}, now), now + timedelta(seconds=30))
        later = (now + timedelta(seconds=120)).replace(microsecond=0)
        from email.utils import format_datetime
        self.assertEqual(ledger.parse_retry_after({'retry-after': format_datetime(later, usegmt=True)}, now), later)
        self.assertEqual(ledger.parse_retry_after({'x-ratelimit-reset-requests': '2m59.56s',
                                                   'x-ratelimit-reset-tokens': '7.66s'}, now),
                         now + timedelta(seconds=7.66))
        body = {'error': {'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '41s'}]}}
        self.assertEqual(ledger.parse_retry_after({}, now, body), now + timedelta(seconds=41))
        self.assertEqual(ledger.parse_retry_after({'retry-after': '999999'}, now), now + timedelta(hours=24))
        self.assertIsNone(ledger.parse_retry_after({}, now))

    def test_groq_cue_grouping_matches_legacy(self):
        cues = cues_from_verbose_json({'words': [{'word': 'Hello', 'start': .1, 'end': .4},
                                                 {'word': 'everyone', 'start': .45, 'end': .9},
                                                 {'word': 'Welcome', 'start': 1.7, 'end': 2.1}]}, 3)
        self.assertEqual([c['text'] for c in cues], ['Hello everyone', 'Welcome'])
        self.assertEqual(cues[0]['timing'], 'groq_word')


class GroqAdapterTests(TestCase):
    def setUp(self):
        self.job = GenerationJob.objects.create(source=SourceVideo.objects.create(source_type='upload'), owner='owner-1')

    def test_text_call_is_ledgered_and_uses_strict_schema(self):
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post',
                                                 return_value=chat({'title': 'A', 'score': .5})) as post:
            text = registry.resolve(Capability.TEXT, job=self.job)
            self.assertIsInstance(text, GroqProvider)
            result = text.complete_json(task='answer', system='Be factual.', user='data', schema=Answer, max_tokens=200)
        self.assertEqual(result, Answer(title='A', score=.5))
        sent = post.call_args.kwargs['json']
        self.assertEqual(sent['response_format']['type'], 'json_schema')      # gpt-oss-20b default is strict
        self.assertEqual(sent['max_completion_tokens'], 200)
        row = ProviderUsage.objects.get()
        self.assertEqual((row.provider, row.capability, row.status, row.billing, row.http_status, row.owner),
                         ('groq', 'text', 'ok', 'free_tier', 200, 'owner-1'))
        self.assertEqual((row.units, row.unit_type), (42, 'tokens'))
        self.assertEqual(ledger.job_calls(self.job), 1)

    def test_strict_400_falls_back_to_json_object_with_two_rows(self):
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post',
                                                 side_effect=[response(400), chat({'title': 'B', 'score': 1})]) as post:
            registry.get('groq', job=self.job).complete_json(task='t', system='s', user='u', schema=Answer, max_tokens=50)
        self.assertEqual([c.kwargs['json']['response_format']['type'] for c in post.call_args_list],
                         ['json_schema', 'json_object'])
        self.assertEqual(sorted(ProviderUsage.objects.values_list('status', flat=True)), ['error', 'ok'])

    def test_429_starts_a_cooldown_honoured_across_calls(self):
        limited = response(429, headers={'retry-after': '30'})
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post', return_value=limited) as post:
            groq = registry.resolve(Capability.TEXT, job=self.job)
            with self.assertRaises(ProviderRateLimited):
                groq.complete_json(task='t', system='s', user='u', schema=Answer, max_tokens=50)
            row = ProviderUsage.objects.get()
            self.assertEqual((row.status, row.http_status), ('rate_limited', 429))
            self.assertAlmostEqual((row.retry_after_until - timezone.now()).total_seconds(), 30, delta=5)
            # Same adapter, and a fresh resolution (a different process would read the same row).
            with self.assertRaises(ProviderRateLimited):
                groq.complete_json(task='t', system='s', user='u', schema=Answer, max_tokens=50)
            with self.assertRaises(ProviderUnavailable) as unavailable:
                registry.resolve(Capability.TEXT, job=self.job)
            self.assertIn('rate limited until', unavailable.exception.message)
            self.assertFalse(registry.usable('groq', Capability.ASR))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(ProviderUsage.objects.filter(status='blocked_by_policy').count(), 1)
        self.assertEqual(ledger.job_calls(self.job), 1)   # blocked attempts are not inference calls

    def test_groq_is_gated_by_free_tier_and_enabled_switches(self):
        for env in ({'AI_ALLOW_FREE_TIER': 'false'}, {'GROQ_ENABLED': 'false'}):
            with patch.dict(os.environ, dict(GROQ, **env)), patch('api.services.groq.httpx.post') as post:
                self.assertFalse(registry.usable('groq', Capability.TEXT))
                self.assertIsNone(registry.get('groq', job=self.job).metadata('Real words were spoken here.'))
                post.assert_not_called()

    def test_metadata_wrapper_counts_calls(self):
        content = {'title': 'The mission route nobody expected', 'description': 'The speaker explains the route.',
                   'hashtags': ['#Gaming', '#Shorts', '#ViceCity']}
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post', return_value=chat(content)):
            result = registry.get('groq', job=self.job).metadata('The speaker explains the route.')
        self.assertEqual(result['title'], 'The mission route nobody expected')
        self.assertEqual(ledger.job_calls(self.job), 1)
        with patch.dict(os.environ, GROQ), patch('api.services.groq.httpx.post',
                                                 side_effect=__import__('httpx').ConnectError('offline')):
            self.assertIsNone(registry.get('groq', job=self.job).metadata('Actual words'))
        self.assertEqual(ProviderUsage.objects.filter(status='error').count(), 1)

    def test_clip_transcription_is_gated_and_grouped(self):
        payload = {'words': [{'word': 'Hello', 'start': .1, 'end': .4}, {'word': 'there', 'start': .45, 'end': .9}]}
        with TemporaryDirectory() as tmp, patch.dict(os.environ, GROQ), \
             patch('api.providers.groq_adapter.run', side_effect=lambda *a, **k: (Path(tmp)/'groq-2.flac').write_bytes(b'x')), \
             patch('api.services.groq.httpx.post', return_value=response(body=payload)) as post:
            cues = registry.get('groq', job=self.job).transcribe_clip(Path(tmp)/'s.mp4', Path(tmp), 5, 3, 2)
            self.assertFalse((Path(tmp)/'groq-2.flac').exists())
        self.assertEqual(cues, [{'start': .1, 'end': .9, 'text': 'Hello there', 'timing': 'groq_word'}])
        self.assertEqual(post.call_args.kwargs['data']['response_format'], 'verbose_json')
        self.assertEqual(ProviderUsage.objects.get().capability, 'asr')


class GeminiAdapterTests(TestCase):
    ENV = dict(CLEAN, GEMINI_API_KEY='g-key', GEMINI_TEXT_MODEL='configured-flash-model')

    def test_requires_key_and_model_from_env(self):
        with patch.dict(os.environ, dict(CLEAN, GEMINI_API_KEY='g-key')):
            reason = registry.get('gemini').availability(Capability.TEXT).reason
        self.assertIn('GEMINI_TEXT_MODEL is not set', reason)

    def test_generate_content_shape_validation_and_ledger(self):
        body = {'candidates': [{'content': {'parts': [{'text': json.dumps({'title': 'G', 'score': .2})}]}}],
                'usageMetadata': {'totalTokenCount': 17}}
        with patch.dict(os.environ, self.ENV), patch('api.providers.gemini.httpx.post', return_value=response(body=body)) as post:
            text = registry.resolve(Capability.TEXT)          # groq has no key, so gemini is next
            result = text.complete_json(task='t', system='Be factual.', user='u', schema=Answer, max_tokens=99)
        self.assertEqual(result.title, 'G')
        url = post.call_args.args[0]
        sent = post.call_args.kwargs['json']
        self.assertTrue(url.endswith('/v1beta/models/configured-flash-model:generateContent'))
        self.assertEqual(post.call_args.kwargs['headers'], {'x-goog-api-key': 'g-key'})
        self.assertEqual(sent['generationConfig']['responseMimeType'], 'application/json')
        self.assertEqual(sent['generationConfig']['maxOutputTokens'], 99)
        self.assertIn('properties', sent['generationConfig']['responseJsonSchema'])
        self.assertEqual(sent['systemInstruction'], {'parts': [{'text': 'Be factual.'}]})
        row = ProviderUsage.objects.get()
        self.assertEqual((row.provider, row.units, row.unit_type), ('gemini', 17, 'tokens'))

    def test_invalid_output_is_never_fabricated(self):
        bad = {'candidates': [{'content': {'parts': [{'text': '{"title": 3}'}]}}]}
        with patch.dict(os.environ, self.ENV), patch('api.providers.gemini.httpx.post', return_value=response(body=bad)):
            with self.assertRaises(StudioError) as ctx:
                registry.get('gemini').complete_json(task='t', system='', user='u', schema=Answer, max_tokens=10)
        self.assertEqual(ctx.exception.code, 'ai_invalid_response')
        self.assertEqual(ProviderUsage.objects.count(), 2)

    def test_retry_info_sets_cooldown(self):
        body = {'error': {'code': 429, 'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '20s'}]}}
        with patch.dict(os.environ, self.ENV), patch('api.providers.gemini.httpx.post', return_value=response(429, body)):
            with self.assertRaises(ProviderRateLimited):
                registry.get('gemini').complete_json(task='t', system='', user='u', schema=Answer, max_tokens=10)
            self.assertIsNotNone(ledger.cooldown_until('gemini'))


class HuggingFaceRegistryTests(TestCase):
    def test_registry_built_hf_ledgers_each_attempt_and_blocks(self):
        job = GenerationJob.objects.create(source=SourceVideo.objects.create(source_type='upload'), owner='o')
        hf = registry.get('huggingface', job=job)
        with patch.dict(os.environ, dict(CLEAN, AI_ALLOW_BILLABLE='true', AI_MAX_RETRIES='1')), \
             patch('api.providers.huggingface.time.sleep'):
            error = RuntimeError('busy'); error.response = Mock(status_code=503)
            with self.assertRaises(StudioError) as ctx:
                hf.request(Mock(side_effect=error))
        self.assertEqual(ctx.exception.code, 'ai_timeout')
        self.assertEqual(list(ProviderUsage.objects.values_list('status', 'http_status')), [('error', 503)] * 2)
        self.assertEqual(ledger.job_calls(job), 2)
        with patch.dict(os.environ, CLEAN), self.assertRaises(StudioError) as ctx:
            hf.request(Mock())
        self.assertEqual(ctx.exception.code, 'paid_inference_disabled')


class RegistryAndEndpointTests(TestCase):
    def test_nothing_configured_means_unavailable_with_reasons(self):
        with patch.dict(os.environ, CLEAN), self.assertRaises(ProviderUnavailable) as ctx:
            registry.resolve(Capability.TEXT)
        self.assertIn('GROQ_API_KEY is not set', ctx.exception.message)
        self.assertIn('GEMINI_API_KEY is not set', ctx.exception.message)
        self.assertEqual(ctx.exception.code, 'ai_unavailable')

    def test_order_is_env_configurable(self):
        with patch.dict(os.environ, dict(CLEAN, AI_PROVIDER_ORDER_TEXT='huggingface, gemini,unknown')):
            self.assertEqual(registry.order(Capability.TEXT), ['huggingface', 'gemini'])
        with patch.dict(os.environ, CLEAN):
            self.assertEqual(registry.order(Capability.ASR)[0], 'local_whisper')
            self.assertEqual(registry.order(Capability.TEXT), ['groq', 'gemini', 'huggingface'])

    def test_manual_narration_is_local_and_honest(self):
        tts = registry.resolve(Capability.TTS)
        self.assertEqual(tts.info.billing, Billing.LOCAL)
        with self.assertRaises(StudioError) as ctx:
            tts.synthesize('hello', voice_id='', speed=1, out=Path('/tmp/never.wav'))
        self.assertEqual(ctx.exception.code, 'manual_narration_required')

    def test_providers_endpoint_reports_unavailable_hosted_providers_without_network(self):
        with patch.dict(os.environ, CLEAN), patch('httpx.post') as post, patch('httpx.get') as get:
            data = Client().get('/api/v1/providers').json()
            post.assert_not_called(); get.assert_not_called()
        names = {p['name']: p for p in data['providers']}
        self.assertTrue({'groq', 'gemini', 'huggingface', 'local_whisper', 'manual'} <= set(names))
        for entry in data['providers']:
            if entry['billing'] != 'local_free':
                self.assertFalse(entry['available'], entry['name'])
                self.assertTrue(entry['reason'].strip(), entry['name'])
        self.assertTrue(names['manual']['available'])
        self.assertIn('human reviewers', names['gemini']['notes'])
        self.assertIn('EEA', names['gemini']['notes'])
        self.assertIn('a paid Groq plan bills', names['groq']['notes'])
        self.assertEqual(data['policy']['daily_budget_usd'], '0')

    def test_capabilities_keep_keys_and_add_providers(self):
        with patch.dict(os.environ, CLEAN):
            data = Client().get('/api/v1/capabilities').json()
        for key in ('hosted_inference_enabled', 'asr', 'translation', 'original_analysis', 'youtube_oauth_configured',
                    'instagram_publishing_available', 'reason', 'providers', 'groq'):
            self.assertIn(key, data)
        self.assertFalse(data['hosted_inference_enabled'] or data['translation'] or data['original_analysis'])
        self.assertFalse(data['groq']['available'])
        with patch.dict(os.environ, dict(CLEAN, AI_ALLOW_BILLABLE='true', HF_TOKEN='t', HF_TEXT_MODEL='m')):
            data = Client().get('/api/v1/capabilities').json()
        self.assertTrue(data['hosted_inference_enabled'] and data['original_analysis'])
        self.assertFalse(data['translation'])
        with patch.dict(os.environ, GROQ):
            groq = Client().get('/api/v1/capabilities').json()['groq']
        self.assertTrue(groq['available'] and groq['text'] and groq['asr'])

    def test_policy_block_raises_typed_error(self):
        with patch.dict(os.environ, CLEAN), self.assertRaises(ProviderPolicyBlocked) as ctx:
            policy.enforce(info(Billing.METERED))
        self.assertEqual(ctx.exception.code, 'paid_inference_disabled')
