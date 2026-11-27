"""Exercise the updater's source selection without importing the running app."""
import ast
from pathlib import Path
import unittest
from unittest.mock import Mock


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).parents[1] / 'xray_updater.py'
        tree = ast.parse(path.read_text())
        tree.body = [n for n in tree.body if isinstance(n, (ast.Import, ast.FunctionDef, ast.ClassDef)) or
                     isinstance(n, ast.Assign)]
        self.ns = {'logger': Mock()}
        exec(compile(tree, str(path), 'exec'), self.ns)
        self.fetch = Mock(side_effect=RuntimeError('download intercepted'))
        self.ns['_fetch'] = self.fetch
        self.ns['_xray_asset_name'] = lambda: 'Xray-linux-64.zip'
        self.release_fn = self.ns['_release']
        self.ns['_release'] = Mock(return_value={'tag_name': 'v26.1.1', 'assets': [
            {'name': 'Xray-linux-64.zip'}, {'name': 'Xray-linux-64.zip.dgst'}]})

    def test_custom_repository(self):
        with self.assertRaisesRegex(RuntimeError, 'intercepted'):
            self.ns['fetch_and_verify']('v26.1.1', 'https://github.com/example/core.git/')
        self.fetch.assert_called_once_with('https://github.com/example/core/releases/download/v26.1.1/Xray-linux-64.zip')

    def test_default_repository(self):
        with self.assertRaises(RuntimeError):
            self.ns['fetch_and_verify']('26.1.1')
        self.assertIn('/XTLS/Xray-core/', self.fetch.call_args.args[0])

    def test_invalid_repository_never_downloaded(self):
        for url in ['http://github.com/a/b', 'https://evil.com/a/b', 'https://github.com/a/b/../../x', 'https://github.com/a/b?x=1']:
            with self.subTest(url=url), self.assertRaises(self.ns['XrayUpdateError']):
                self.ns['fetch_and_verify']('26.1.1', url)
        self.fetch.assert_not_called()

    def test_checksum_mismatch(self):
        with self.assertRaises(self.ns['XrayUpdateError']):
            self.ns['_verify_checksum'](b'wrong', 'SHA256= 0000', 'xray.zip')

    def test_exact_tag_and_numeric_fallback(self):
        import json
        for tag in ['26.1.1', 'v26.11.9-h2Flow-7', '26.1.1-custom']:
            self.fetch.reset_mock()
            self.fetch.side_effect = None
            self.fetch.return_value = json.dumps({'tag_name': tag}).encode()
            self.assertEqual(self.release_fn('https://github.com/a/b', tag)['tag_name'], tag)
            self.fetch.assert_called_once_with('https://api.github.com/repos/a/b/releases/tags/' + tag)
        self.fetch.reset_mock()
        self.fetch.side_effect = [self.ns['ReleaseNotFound']('404'), b'{"tag_name":"v26.1.1"}']
        self.assertEqual(self.release_fn('https://github.com/a/b', '26.1.1')['tag_name'], 'v26.1.1')
        self.assertEqual(self.fetch.call_count, 2)

    def test_fork_raw_binary_and_checksum(self):
        import hashlib
        import tempfile
        from unittest.mock import patch
        binary = b'fake binary for mocked version probe'
        digest = hashlib.sha256(binary).hexdigest()
        for machine, asset in [('x86_64', 'xray-linux-amd64'), ('aarch64', 'xray-linux-arm64')]:
            with self.subTest(machine=machine), tempfile.TemporaryDirectory() as directory:
                self.ns['XRAY_EXECUTABLE_PATH'] = directory + '/installed'
                self.ns['_release'].return_value = {'tag_name': 'v26.11.9-h2Flow-7', 'assets': [
                    {'name': asset}, {'name': 'sha256sums.txt'}]}
                self.fetch.side_effect = [binary, (digest + '  ' + asset + '\n').encode()]
                with patch.object(self.ns['platform'], 'machine', return_value=machine), patch.object(
                    self.ns['subprocess'], 'check_output', return_value=b'Xray 26.11.9 (Xray, Penetrates Everything.)'):
                    prepared = self.ns['fetch_and_verify']('v26.11.9-h2Flow-7', 'https://github.com/Medium1992/Xray-core-fork')
                try:
                    self.assertEqual(Path(prepared.binary_path).read_bytes(), binary)
                    self.assertIn('/v26.11.9-h2Flow-7/sha256sums.txt', self.fetch.call_args.args[0])
                finally:
                    prepared.cleanup()

    def test_checksum_does_not_accept_another_asset(self):
        import hashlib
        digest = hashlib.sha256(b'file').hexdigest()
        with self.assertRaises(self.ns['XrayUpdateError']):
            self.ns['_verify_checksum'](b'file', digest + '  xray-linux-arm64', 'xray-linux-amd64')

    def test_reject_path_tags(self):
        for tag in ['../foo', 'v1/../../x', 'tag?x=1', '']:
            with self.assertRaises(self.ns['XrayUpdateError']):
                self.ns['_validate_version'](tag)
