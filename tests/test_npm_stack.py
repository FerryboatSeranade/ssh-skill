from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import support  # noqa: F401
import npm_stack as npm


class NpmStackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'stack'
        self.args = npm.parser().parse_args(['npm', 'install', '--directory', str(self.root)])
        self.secret = Path(self.temp.name) / 'private.json'
        npm.private_json(self.secret, {'email': 'test@example.org', 'password': 'test-$secret-pass'})
        self.args.credentials_file = str(self.secret)

    def test_preview_never_executes_or_creates_files(self):
        with patch.object(npm, 'run') as run:
            result = npm.execute(self.args)
        self.assertTrue(result['preview'])
        self.assertFalse(self.root.exists())
        run.assert_not_called()

    def test_invalid_ports_and_purge_fail_before_any_mutation(self):
        for port in [80, 443, 0, 65536]:
            self.args.admin_port = port
            with self.assertRaises(npm.Failure):
                npm.execute(self.args)
        self.args.admin_port = 81
        self.args.purge_data = True
        with self.assertRaises(npm.Failure):
            npm.execute(self.args)

    def test_secret_permissions_are_required(self):
        self.secret.chmod(0o644)
        with self.assertRaises(npm.Failure):
            npm.credentials(self.secret)

    def test_symlink_root_and_unmanaged_data_are_rejected(self):
        target = Path(self.temp.name) / 'target'
        target.mkdir()
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaises(npm.Failure):
            npm.safe_root(str(self.root))
        self.root.unlink()
        self.root.mkdir()
        (self.root / 'existing').write_text('keep')
        with patch.object(npm, 'docker_ready'), self.assertRaises(npm.Failure):
            npm.npm_install(self.args, self.root)
        self.assertEqual('keep', (self.root / 'existing').read_text())

    def test_port_conflict_prevents_creation(self):
        with patch.object(npm, 'docker_ready'), patch.object(npm, 'port_conflicts', return_value=[{'port': 80}]):
            with self.assertRaises(npm.Failure):
                npm.npm_install(self.args, self.root)
        self.assertFalse(self.root.exists())

    def install(self, *, fail_login=False):
        seen = []
        def compose(root, *args, overlay=None):
            if overlay:
                doc = json.loads(overlay.read_text())
                self.assertEqual({'driver': 'none'}, doc['services']['app']['logging'])
                self.assertEqual('test-$$secret-pass', doc['services']['app']['environment']['INITIAL_ADMIN_PASSWORD'])
                self.assertEqual(0, overlay.stat().st_mode & 0o077)
            seen.append(args)
        with patch.object(npm, 'docker_ready'), patch.object(npm, 'port_conflicts', return_value=[]), \
             patch.object(npm, 'compose', side_effect=compose), \
             patch.object(npm, 'wait_web', side_effect=npm.Failure('readiness failed') if fail_login else None):
            if fail_login:
                with self.assertRaises(npm.Failure):
                    npm.npm_install(self.args, self.root)
            else:
                npm.npm_install(self.args, self.root)
        return seen

    def test_bootstrap_cleanup_and_idempotency(self):
        calls = self.install()
        self.assertIn(('rm', '--stop', '--force', 'app'), calls)
        self.assertFalse((self.root / '.bootstrap.json').exists())
        for path in self.root.iterdir():
            self.assertNotIn('test-$secret-pass', path.read_text())
        with patch.object(npm, 'docker_ready'), patch.object(npm, 'compose') as compose, patch.object(npm, 'wait_web'):
            result = npm.npm_install(self.args, self.root)
        self.assertFalse(result['changed'])
        compose.assert_called_once_with(self.root, 'up', '-d')

    def test_bootstrap_failure_removes_secret_and_allows_explicit_recovery(self):
        calls = self.install(fail_login=True)
        self.assertIn(('rm', '--stop', '--force', 'app'), calls)
        self.assertFalse((self.root / '.bootstrap.json').exists())
        self.assertEqual('initializing', npm.load_managed(self.root)['state'])
        self.install()
        self.assertEqual('ready', npm.load_managed(self.root)['state'])

    def test_uninstall_retains_data_and_rejects_modified_compose(self):
        self.install()
        data = self.root / 'data'
        data.mkdir()
        (data / 'database.sqlite').write_text('valuable data')
        with patch.object(npm, 'docker_ready'), patch.object(npm, 'compose'):
            npm.npm_uninstall(self.args, self.root)
        self.assertTrue((data / 'database.sqlite').exists())
        (self.root / 'compose.yaml').write_text('{}')
        with patch.object(npm, 'compose') as compose, self.assertRaises(npm.Failure):
            npm.npm_uninstall(self.args, self.root)
        compose.assert_not_called()

    def test_purge_requires_owned_tree_and_rejects_symlinks(self):
        self.install()
        (self.root / 'linked').symlink_to(self.secret)
        self.args.purge_data = True
        with patch.object(npm, 'docker_ready'), patch.object(npm, 'compose'), self.assertRaises(npm.Failure):
            npm.npm_uninstall(self.args, self.root)
        self.assertTrue(self.secret.exists())

    def test_docker_uninstall_refuses_any_existing_container(self):
        with patch.object(npm, 'os_release'), patch.object(npm.shutil, 'which', return_value='/bin/docker'), \
             patch.object(npm, 'docker_ready'), patch.object(npm, 'run', return_value=SimpleNamespace(stdout='stopped-id')) as run:
            with self.assertRaises(npm.Failure):
                npm.docker_uninstall()
        run.assert_called_once_with(['docker', 'ps', '-aq'])

    def test_subprocess_errors_and_main_do_not_expose_secrets(self):
        result = SimpleNamespace(returncode=1, stdout='test-$secret-pass', stderr='test-$secret-pass')
        with patch.object(npm.subprocess, 'run', return_value=result), self.assertRaises(npm.Failure) as error:
            npm.run(['docker', 'compose', 'up'])
        self.assertNotIn('secret', str(error.exception))
        with patch.object(npm, 'execute', side_effect=ValueError('test-$secret-pass')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(1, npm.main(['npm', 'install']))
        self.assertNotIn('secret', out.getvalue())


if __name__ == '__main__':
    unittest.main()
