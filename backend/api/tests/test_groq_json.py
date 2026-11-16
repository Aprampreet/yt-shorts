"""Groq structured output: strict schemas, error surfacing, json_validate_failed and 429 handling. No network."""
import json
import os
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from pydantic import BaseModel

from ..models import GenerationJob, ProviderUsage, SourceVideo
from ..providers import ProviderInvalid, ProviderRateLimited, ProviderUnavailable, ledger, registry
from ..providers import groq_adapter
from ..providers.base import strict_schema
from ..story.schemas import ConceptsOut, IntelligenceOut, ScriptOut

KEY = 'gsk_live_secret_value_123456'
GROQ = {'GROQ_API_KEY': KEY, 'GROQ_ENABLED': 'true', 'GROQ_TEXT_MODEL': '', 'AI_ALLOW_FREE_TIER': '',
        'GROQ_REASONING_EFFORT': '', 'GROQ_RATE_LIMIT_MAX_WAIT': '', 'GROQ_REQUEST_TOKEN_BUDGET': '',
        'AI_FREE_TIER_DAILY_CALLS': '', 'AI_MAX_CALLS_PER_JOB': ''}
BANNED = {'minLength', 'maxLength', 'minItems', 'maxItems', 'default', 'pattern', 'format', 'title', 'examples',
          'minimum', 'maximum', 'const', 'oneOf', 'allOf'}
POST = 'api.services.groq.httpx.post'
SLEEP = 'api.providers.groq_adapter.time.sleep'


def response(status=200, body=None, headers=None):
    mocked = Mock(status_code=status, headers=headers or {})
    mocked.json.return_value = body if body is not None else {}
    return mocked


def chat(content, tokens=42):
    return response(body={'choices': [{'message': {'content': json.dumps(content)}}], 'usage': {'total_tokens': tokens}})


def error(status, code, message, failed=None, headers=None):
    body = {'error': {'message': message, 'type': 'invalid_request_error', 'code': code}}
    if failed:
        body['error']['failed_generation'] = failed
    return response(status, body, headers)


class Answer(BaseModel):
    title: str
    score: float


GOOD = {'title': 'A', 'score': .5}
TOKEN_CAP = error(400, 'json_validate_failed', "Failed to generate JSON. Please adjust your prompt. See 'failed_generation' for more details.",
                  'max completion tokens reached before generating a valid document')


def walk(node, path='$'):
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f'{path}.{key}')
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from walk(value, f'{path}[{i}]')


class StrictSchemaTests(SimpleTestCase):
    def assert_strict(self, model):
        schema = strict_schema(model)
        objects = 0
        for path, node in walk(schema):
            if path.endswith('.properties') or path.endswith('.$defs'):
                continue                     # these map names to sub-schemas; the names are not keywords
            self.assertFalse(BANNED & set(node), f'{model.__name__} {path} keeps {BANNED & set(node)}')
            if node.get('type') == 'object' or 'properties' in node:
                objects += 1
                self.assertEqual(sorted(node['required']), sorted(node['properties']), path)
                self.assertIs(node['additionalProperties'], False, path)
            if '$ref' in node:
                self.assertEqual(list(node), ['$ref'], path)
                self.assertIn(node['$ref'].split('/')[-1], schema['$defs'])
        self.assertGreaterEqual(objects, 2)
        return schema

    def test_story_schemas_meet_groq_strict_rules(self):
        for model in (ConceptsOut, ScriptOut, IntelligenceOut):
            with self.subTest(model=model.__name__):
                self.assert_strict(model)

    def test_defaults_become_required_but_keep_their_type(self):
        concept = strict_schema(ConceptsOut)['$defs']['ConceptOut']
        self.assertIn('claim_ids', concept['required'])
        self.assertEqual(concept['properties']['claim_ids'], {'type': 'array', 'items': {'type': 'string'}})
        claim = strict_schema(IntelligenceOut)['$defs']['ClaimOut']
        self.assertEqual(claim['properties']['quote'], {'type': 'string'})
        self.assertEqual(claim['properties']['kind'], {'type': 'string', 'enum': ['stated', 'inferred']})

    def test_nullable_fields_keep_their_null_branch(self):
        class Maybe(BaseModel):
            note: str | None = None
        schema = strict_schema(Maybe)
        self.assertEqual(schema['required'], ['note'])
        self.assertEqual(schema['properties']['note'], {'anyOf': [{'type': 'string'}, {'type': 'null'}]})

    def test_helper_is_pure(self):
        source = ConceptsOut.model_json_schema()
        strict_schema(source)
        self.assertEqual(source, ConceptsOut.model_json_schema())


class GroqJsonTests(TestCase):
    def setUp(self):
        self.job = GenerationJob.objects.create(source=SourceVideo.objects.create(source_type='upload'), owner='o')
        groq_adapter._STRICT_REJECTED.clear()

    def call(self, responses, env=None, schema=Answer, max_tokens=500):
        with patch.dict(os.environ, dict(GROQ, **(env or {}))), patch(POST, side_effect=responses) as post, \
                patch(SLEEP) as sleep:
            try:
                return registry.get('groq', job=self.job).complete_json(
                    task='story_concepts', system='s', user='u', schema=schema, max_tokens=max_tokens), post, sleep
            except Exception as exc:
                exc.post, exc.sleep = post, sleep
                raise

    @staticmethod
    def sent(post):
        return [c.kwargs['json'] for c in post.call_args_list]

    def test_strict_request_uses_the_strict_subset(self):
        result, post, _ = self.call([chat(GOOD)], schema=Answer)
        self.assertEqual(result, Answer(**GOOD))
        fmt = self.sent(post)[0]['response_format']
        self.assertEqual(fmt['type'], 'json_schema')
        self.assertEqual(fmt['json_schema']['schema'], strict_schema(Answer))
        self.assertNotIn('title', fmt['json_schema']['schema'])

    def test_schema_compile_400_falls_back_once_and_logs_once_per_schema(self):
        rejected = error(400, 'invalid_request_error', "invalid JSON schema for response_format: 'story_concepts': nope")
        with self.assertLogs('api.providers.groq_adapter', 'WARNING') as logs:
            _, post, _ = self.call([rejected, chat(GOOD)])
            _, post2, _ = self.call([rejected, chat(GOOD)])
        self.assertEqual([s['response_format']['type'] for s in self.sent(post)], ['json_schema', 'json_object'])
        self.assertEqual([s['response_format']['type'] for s in self.sent(post2)], ['json_schema', 'json_object'])
        strict_logs = [line for line in logs.output if 'rejected the strict schema' in line]
        self.assertEqual(len(strict_logs), 1)
        self.assertIn('invalid JSON schema for response_format', strict_logs[0])

    def test_json_validate_failed_retries_once_with_headroom_then_succeeds(self):
        result, post, _ = self.call([TOKEN_CAP, chat(GOOD)], env={'GROQ_REASONING_EFFORT': 'high'}, max_tokens=2500)
        self.assertEqual(result, Answer(**GOOD))
        first, second = self.sent(post)
        self.assertEqual((first['reasoning_effort'], second['reasoning_effort']), ('high', 'low'))
        self.assertEqual(first['max_completion_tokens'], 2500)
        self.assertEqual(second['max_completion_tokens'], 3750)          # x1.5, inside the 6500 budget
        self.assertEqual(second['response_format']['type'], 'json_schema')
        self.assertEqual(sorted(ProviderUsage.objects.values_list('status', flat=True)), ['invalid_response', 'ok'])

    def test_headroom_is_capped_by_the_request_budget(self):
        _, post, _ = self.call([TOKEN_CAP, chat(GOOD)], env={'GROQ_REQUEST_TOKEN_BUDGET': '3000'}, max_tokens=2500)
        self.assertLessEqual(self.sent(post)[1]['max_completion_tokens'], 3000)
        self.assertGreaterEqual(self.sent(post)[1]['max_completion_tokens'], 2500)

    def test_json_validate_failed_twice_raises_invalid_with_groq_message(self):
        with self.assertRaises(ProviderInvalid) as caught:
            self.call([TOKEN_CAP, TOKEN_CAP, chat(GOOD)])
        exc = caught.exception
        self.assertEqual(exc.post.call_count, 2)
        self.assertEqual(exc.code, 'ai_invalid_response')
        self.assertIn('HTTP 400 (json_validate_failed): Failed to generate JSON', exc.message)
        self.assertIn('max completion tokens reached', exc.message)

    def test_other_400_does_not_retry_and_shows_groq_text_without_secrets(self):
        bad = error(400, 'invalid_request_error',
                    f'Bad parameter. Authorization: Bearer {KEY} key={KEY} ' + 'x' * 600)
        with self.assertLogs('api.providers.groq_adapter', 'WARNING') as logs, self.assertRaises(ProviderUnavailable) as caught:
            self.call([bad, chat(GOOD)], env={'GROQ_TEXT_MODEL': 'llama-3.3-70b-versatile'})
        exc = caught.exception
        self.assertEqual(exc.post.call_count, 1)
        self.assertEqual((exc.http_status, exc.code), (400, 'ai_unavailable'))
        self.assertIn('Groq returned HTTP 400 (invalid_request_error): Bad parameter.', exc.message)
        self.assertNotIn(KEY, exc.message)
        self.assertNotIn(KEY, '\n'.join(logs.output))
        self.assertNotIn('Bearer gsk', exc.message)
        self.assertLess(len(exc.message), 420)

    def test_short_429_waits_and_retries_once_without_a_cooldown(self):
        limited = error(429, 'rate_limit_exceeded', 'Rate limit reached for model `openai/gpt-oss-20b` on tokens per '
                        'minute (TPM): Limit 8000, Used 6215, Requested 4133. Please try again in 17.61s.')
        result, post, sleep = self.call([limited, chat(GOOD)])
        self.assertEqual(result, Answer(**GOOD))
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 18.61, places=2)
        self.assertEqual(sorted(ProviderUsage.objects.values_list('status', flat=True)), ['ok', 'rate_limited'])
        self.assertIsNone(ledger.cooldown_until('groq'))
        self.assertEqual(ledger.job_calls(self.job), 2)

    def test_second_short_429_is_not_absorbed(self):
        limited = response(429, {'error': {'message': 'slow down', 'code': 'rate_limit_exceeded'}}, {'retry-after': '5'})
        with self.assertRaises(ProviderRateLimited) as caught:
            self.call([limited, limited, chat(GOOD)])
        self.assertEqual(caught.exception.post.call_count, 2)
        caught.exception.sleep.assert_called_once()
        self.assertIn('Wait about 5 s', caught.exception.message)
        self.assertIsNotNone(ledger.cooldown_until('groq'))

    def test_long_429_raises_without_sleeping_and_says_how_long(self):
        limited = error(429, 'rate_limit_exceeded', 'Rate limit reached. Please try again in 2m5.5s.')
        with self.assertRaises(ProviderRateLimited) as caught:
            self.call([limited, chat(GOOD)])
        exc = caught.exception
        exc.sleep.assert_not_called()
        self.assertEqual(exc.post.call_count, 1)
        self.assertIn('Wait about 126 s and retry', exc.message)
        self.assertAlmostEqual((exc.retry_after - ledger.timezone.now()).total_seconds(), 125.5, delta=5)
        self.assertIsNotNone(ledger.cooldown_until('groq'))

    def test_wait_is_cancelled_with_the_job(self):
        limited = error(429, 'rate_limit_exceeded', 'Please try again in 3s.')
        from ..pipeline.context import Cancelled

        def cancel(_):
            GenerationJob.objects.filter(pk=self.job.pk).update(status='cancelled')
        with patch.dict(os.environ, GROQ), patch(POST, side_effect=[limited, chat(GOOD)]) as post, \
                patch(SLEEP, side_effect=cancel), self.assertRaises(Cancelled):
            registry.get('groq', job=self.job).complete_json(task='t', system='s', user='u', schema=Answer, max_tokens=50)
        self.assertEqual(post.call_count, 1)

    def test_reasoning_effort_sent_for_gpt_oss_only(self):
        _, post, _ = self.call([chat(GOOD)])
        sent = self.sent(post)[0]
        self.assertEqual((sent['reasoning_effort'], sent['include_reasoning']), ('low', False))
        _, post, _ = self.call([chat(GOOD)], env={'GROQ_TEXT_MODEL': 'llama-3.3-70b-versatile'})
        sent = self.sent(post)[0]
        self.assertNotIn('reasoning_effort', sent)
        self.assertNotIn('include_reasoning', sent)
        self.assertEqual(sent['response_format'], {'type': 'json_object'})

    def test_reasoning_effort_env_override_and_invalid_value(self):
        for value, expected in (('medium', 'medium'), (' HIGH ', 'high'), ('extreme', 'low'), ('', 'low')):
            with self.subTest(value=value):
                _, post, _ = self.call([chat(GOOD)], env={'GROQ_REASONING_EFFORT': value,
                                                          'GROQ_TEXT_MODEL': 'openai/gpt-oss-120b'})
                self.assertEqual(self.sent(post)[0]['reasoning_effort'], expected)

    def test_rate_limit_absorb_can_be_disabled(self):
        limited = error(429, 'rate_limit_exceeded', 'Please try again in 3s.')
        with self.assertRaises(ProviderRateLimited) as caught:
            self.call([limited, chat(GOOD)], env={'GROQ_RATE_LIMIT_MAX_WAIT': '0'})
        caught.exception.sleep.assert_not_called()
