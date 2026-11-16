import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock

from api.apps import watch_env_file
from backend.envfile import MARKER, load_dotenv


class EnvFileTests(TestCase):
    def write(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / '.env'
        p.write_text(text)
        return p

    def test_parsing_rules(self):
        p = self.write('# c\n\nnoequals\nA = \tone\nB="two"\nC=\'three\'\n1BAD=x\nbad name=x\nD="\n')
        env = {}
        load_dotenv(p, env)
        self.assertEqual(env['A'], 'one')
        self.assertEqual(env['B'], 'two')
        self.assertEqual(env['C'], 'three')
        self.assertEqual(env['D'], '"')
        self.assertNotIn('1BAD', env)
        self.assertNotIn('bad name', env)
        self.assertNotIn('noequals', env)

    def test_terminal_value_wins(self):
        p = self.write('A=file\n')
        env = {'A': 'term'}
        load_dotenv(p, env)
        self.assertEqual(env['A'], 'term')
        self.assertNotIn(MARKER, env)

    def test_first_load_records_marker(self):
        p = self.write('A=1\nB=2\n')
        env = {}
        self.assertEqual(load_dotenv(p, env), ['A', 'B'])
        self.assertEqual(env[MARKER], 'A,B')

    def test_child_picks_up_edit(self):
        p = self.write('A=old\n')
        env = {}
        load_dotenv(p, env)
        child = dict(env)
        p.write_text('A=new\n')
        load_dotenv(p, child)
        self.assertEqual(child['A'], 'new')

    def test_removed_name_is_removed(self):
        p = self.write('A=1\nB=2\n')
        env = {}
        load_dotenv(p, env)
        child = dict(env)
        p.write_text('A=1\n')
        load_dotenv(p, child)
        self.assertNotIn('B', child)
        self.assertEqual(child[MARKER], 'A')

    def test_terminal_value_not_overridden_in_child(self):
        p = self.write('A=file\nB=file\n')
        env = {'A': 'term'}
        load_dotenv(p, env)
        child = dict(env)
        p.write_text('A=edited\nB=edited\n')
        load_dotenv(p, child)
        self.assertEqual(child['A'], 'term')
        self.assertEqual(child['B'], 'edited')

    def test_empty_value_is_overridden(self):
        p = self.write('A=file\n')
        env = {'A': ''}
        load_dotenv(p, env)
        self.assertEqual(env['A'], 'file')

    def test_missing_file_is_noop(self):
        env = {'X': '1'}
        self.assertEqual(load_dotenv(Path('/nonexistent/.env'), env), [])
        self.assertEqual(env, {'X': '1'})


class WatchHookTests(TestCase):
    def test_handler_watches_env_file(self):
        from django.conf import settings
        sender = MagicMock()
        watch_env_file(sender=sender)
        sender.watch_file.assert_called_once_with(settings.ENV_FILE)
        self.assertEqual(Path(settings.ENV_FILE).name, '.env')
