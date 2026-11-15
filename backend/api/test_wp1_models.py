"""WP1 foundation tests: models, constraints, backfill migration, states, queue routing."""
import os
import runpy
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from .models import (GeneratedScript, GeneratedShort, GeneratedVideo, GenerationJob, GenerationProject, MediaAsset,
                     ScheduledPost, SourceVideo, StoryboardScene, StoryConcept)
from .pipeline import ensure_project, states

SETTINGS_FILE = Path(settings.BASE_DIR) / 'backend' / 'settings.py'


def env_file_sets(*names):
    """settings.py fills unset variables from the project .env; defaults can only be asserted when it doesn't."""
    env_file = Path(settings.BASE_DIR).parent / '.env'
    if not env_file.is_file():
        return False
    lines = [line.split('=', 1) for line in env_file.read_text().splitlines() if '=' in line and not line.lstrip().startswith('#')]
    return any(key.strip() in names and value.strip() for key, value in lines)


class BackfillMigrationTests(TransactionTestCase):
    before = [('api', '0008_generationproject_job_fields')]
    after_backfill = [('api', '0009_backfill_projects')]
    latest = [('api', '0011_generatedshort_video_fields_scheduledpost_platform_metadata')]

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        self.migrate(self.latest)

    def test_backfill_creates_one_shorts_project_per_job_and_reverses(self):
        apps = self.migrate(self.before)
        Source = apps.get_model('api', 'SourceVideo')
        Job = apps.get_model('api', 'GenerationJob')
        Short = apps.get_model('api', 'GeneratedShort')
        source = Source.objects.create(title='Original upload title')
        done = Job.objects.create(source=source, owner='owner-a', status='completed', completed_at=timezone.now())
        old = timezone.now() - timezone.timedelta(days=3)
        Job.objects.filter(pk=done.pk).update(created_at=old)
        running = Job.objects.create(source=source, owner='owner-b', status='rendering')
        sourceless = Job.objects.create(source=None, owner='owner-c', status='failed')
        Short.objects.create(job=done, short_number=1, start_time=0, end_time=10, duration=10, title='t', video_file='x.mp4')

        apps = self.migrate(self.after_backfill)
        Job = apps.get_model('api', 'GenerationJob')
        Project = apps.get_model('api', 'GenerationProject')
        self.assertEqual(Project.objects.count(), 3)
        self.assertFalse(Job.objects.filter(project__isnull=True).exists())
        done_project = Job.objects.get(pk=done.pk).project
        self.assertEqual((done_project.kind, done_project.owner, done_project.title, done_project.status),
                         ('shorts', 'owner-a', 'Original upload title', 'completed'))
        self.assertEqual(done_project.created_at, old)
        self.assertEqual(done_project.source_id, source.pk)
        self.assertEqual(Job.objects.get(pk=running.pk).project.status, 'active')
        self.assertEqual(Job.objects.get(pk=sourceless.pk).project.title, '')

        apps = self.migrate(self.latest)
        Short = apps.get_model('api', 'GeneratedShort')
        short = Short.objects.get(job_id=done.pk)
        self.assertEqual(short.project_id, done_project.pk)
        self.assertEqual(short.kind, 'highlight')

        apps = self.migrate(self.before)
        Job = apps.get_model('api', 'GenerationJob')
        Project = apps.get_model('api', 'GenerationProject')
        self.assertEqual(Project.objects.count(), 0)
        self.assertEqual(Job.objects.count(), 3, 'reverse must never delete jobs')
        self.assertFalse(Job.objects.filter(project__isnull=False).exists())

    def test_reverse_keeps_projects_that_hold_story_jobs(self):
        apps = self.migrate(self.after_backfill)
        Job = apps.get_model('api', 'GenerationJob')
        Project = apps.get_model('api', 'GenerationProject')
        project = Project.objects.create(owner='o', kind='shorts')
        Job.objects.create(owner='o', project=project, pipeline='story')
        apps = self.migrate(self.before)
        self.assertTrue(apps.get_model('api', 'GenerationProject').objects.filter(pk=project.pk).exists())


class ConstraintTests(TestCase):
    def asset(self, **kw):
        values = dict(owner='o', kind='image', file='assets/a.png', sha256='a' * 64)
        values.update(kw)
        return MediaAsset.objects.create(**values)

    def test_license_status_defaults_to_unknown_and_has_six_choices(self):
        self.assertEqual(self.asset().license_status, 'unknown')
        self.assertEqual([c for c, _ in MediaAsset._meta.get_field('license_status').choices],
                         ['owned', 'commercial_use_permitted', 'attribution_required', 'permission_pending', 'unknown', 'restricted'])

    def test_attribution_required_needs_attribution_text(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.asset(license_status='attribution_required')
        self.assertEqual(self.asset(license_status='attribution_required', attribution_text='Photo: A. Person, CC BY 4.0').license_status,
                         'attribution_required')

    def test_sha256_unique_per_owner_only_for_live_assets(self):
        first = self.asset()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.asset()
        self.asset(owner='someone-else')
        first.deleted_at = timezone.now()
        first.save()
        self.asset()  # re-upload after soft delete is allowed
        self.asset(sha256='', file='a1')
        self.asset(sha256='', file='a2')  # unhashed rows never collide

    def test_scene_end_must_exceed_start_and_assets_are_protected(self):
        project = GenerationProject.objects.create(owner='o', kind='story')
        concept = StoryConcept.objects.create(project=project, index=0, angle='explainer', title='c')
        script = GeneratedScript.objects.create(project=project, concept=concept)
        asset = self.asset()
        StoryboardScene.objects.create(script=script, order=0, start=0, end=2.5, visual_type='asset_image', asset=asset)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StoryboardScene.objects.create(script=script, order=1, start=3, end=3, visual_type='title_card')
        with self.assertRaises(ProtectedError):
            asset.delete()

    def test_manual_script_without_concept(self):
        project = GenerationProject.objects.create(owner='o', kind='story')
        self.assertIsNone(GeneratedScript.objects.create(project=project, full_text='user script').concept)


class ProjectHelperTests(TestCase):
    def test_ensure_project_is_idempotent_and_links_outputs(self):
        source = SourceVideo.objects.create(title='My clip')
        job = GenerationJob.objects.create(source=source, owner='o')
        short = GeneratedShort.objects.create(job=job, short_number=1, start_time=0, end_time=5, duration=5, title='t', video_file='v.mp4')
        project = ensure_project(job)
        self.assertEqual((project.kind, project.owner, project.title, project.source_id), ('shorts', 'o', 'My clip', source.pk))
        self.assertEqual(ensure_project(job), project)
        stale = GenerationJob.objects.get(pk=job.pk)
        stale.project_id = None  # a copy loaded before the first call
        self.assertEqual(ensure_project(stale), project)
        self.assertEqual(GenerationProject.objects.count(), 1)
        short.refresh_from_db()
        self.assertEqual(short.project_id, project.pk)

    def test_ensure_project_works_without_source_and_rejects_story_jobs(self):
        self.assertEqual(ensure_project(GenerationJob.objects.create(owner='o')).title, '')
        with self.assertRaises(ValueError):
            ensure_project(GenerationJob.objects.create(owner='o', pipeline='story'))

    def test_generated_video_proxy_only_sees_story_outputs(self):
        job = GenerationJob.objects.create(owner='o', pipeline='story')
        GeneratedShort.objects.create(job=job, short_number=1, start_time=0, end_time=5, duration=5, title='h', video_file='h.mp4')
        story = GeneratedVideo.objects.create(job=job, short_number=2, start_time=0, end_time=30, duration=30, title='s', video_file='s.mp4')
        self.assertEqual(story.kind, 'story')
        self.assertEqual(list(GeneratedVideo.objects.values_list('pk', flat=True)), [story.pk])
        self.assertEqual(GeneratedShort.objects.filter(job=job).count(), 2)

    def test_scheduled_post_platform_metadata_defaults_to_dict(self):
        self.assertEqual(ScheduledPost.objects.create(owner='o', platform='instagram', title='t', description='d').platform_metadata, {})


class StateTests(SimpleTestCase):
    def test_terminal_matches_tasks(self):
        from .tasks import TERMINAL
        self.assertEqual(set(states.TERMINAL), set(TERMINAL))

    def test_phase_mapping(self):
        expected = {'preprocessing': 'analyzing', 'selecting': 'analyzing', 'editing': 'rendering',
                    'captioning': 'rendering', 'validating': 'quality_checking'}
        for legacy, canonical in expected.items():
            self.assertEqual(states.phase(legacy), canonical)
            self.assertFalse(states.is_canonical(legacy))
        for state in states.CANONICAL:
            self.assertEqual(states.phase(state), state)
        self.assertEqual(states.phase('something_new'), 'something_new')
        self.assertTrue(states.is_terminal('needs_review'))
        self.assertTrue(states.is_active('captioning'))
        self.assertEqual(set(states.STEP_STATES), set(states.STORY_STEPS))
        self.assertTrue(set(states.STEP_STATES.values()) <= set(states.PIPELINE_STATES['story']))


class QueueRoutingTests(SimpleTestCase):
    def load_settings(self, **env):
        with patch.dict(os.environ, {'DJANGO_DEBUG': 'true', **env}):
            return runpy.run_path(str(SETTINGS_FILE))

    def test_routes_inactive_by_default(self):
        if env_file_sets('CELERY_SEPARATE_QUEUES'):
            self.skipTest('local .env sets CELERY_SEPARATE_QUEUES')
        env = {k: v for k, v in os.environ.items() if k != 'CELERY_SEPARATE_QUEUES'}
        with patch.dict(os.environ, env, clear=True):
            loaded = self.load_settings()
        self.assertFalse(loaded['CELERY_SEPARATE_QUEUES'])
        self.assertEqual(loaded['CELERY_TASK_ROUTES'], {})
        self.assertEqual(self.load_settings(CELERY_SEPARATE_QUEUES='false')['CELERY_TASK_ROUTES'], {})

    def test_routes_active_when_opted_in(self):
        from celery.app.routes import MapRoute
        routes = self.load_settings(CELERY_SEPARATE_QUEUES='true')['CELERY_TASK_ROUTES']
        route = MapRoute(routes)
        self.assertEqual(route('api.tasks.generate_job')['queue'], 'media')
        self.assertEqual(route('api.tasks.rerender_short')['queue'], 'media')
        self.assertEqual(route('api.story.render')['queue'], 'media')
        self.assertEqual(route('api.story.analyze')['queue'], 'ai')
        self.assertEqual(route('api.tasks.publish_youtube_post')['queue'], 'publish')
        self.assertEqual(route('api.publishing.publish_instagram_post')['queue'], 'publish')

    def test_celery_imports_only_lists_existing_modules(self):
        for module in self.load_settings()['CELERY_IMPORTS']:
            base = Path(settings.BASE_DIR) / module.replace('.', '/')
            self.assertTrue(base.with_suffix('.py').is_file() or (base / '__init__.py').is_file(), module)

    def test_ai_policy_env_defaults_and_legacy_flag(self):
        if env_file_sets('AI_ALLOW_BILLABLE', 'AI_ALLOW_PAID_PROVIDERS', 'AI_ALLOW_FREE_TIER', 'AI_DAILY_BUDGET_USD'):
            self.skipTest('local .env sets AI policy variables')
        env = {k: v for k, v in os.environ.items() if not k.startswith('AI_ALLOW') and k != 'AI_DAILY_BUDGET_USD'}
        with patch.dict(os.environ, env, clear=True):
            loaded = self.load_settings()
            self.assertEqual((loaded['AI_ALLOW_PAID_PROVIDERS'], loaded['AI_ALLOW_FREE_TIER'], loaded['AI_DAILY_BUDGET_USD']),
                             (False, True, 0))
            self.assertTrue(self.load_settings(AI_ALLOW_BILLABLE='true')['AI_ALLOW_PAID_PROVIDERS'])
            self.assertTrue(self.load_settings(AI_ALLOW_PAID_PROVIDERS='true')['AI_ALLOW_PAID_PROVIDERS'])
