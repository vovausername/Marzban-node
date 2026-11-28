import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xray_source


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / 'xray')
        Path(self.path).write_bytes(b'installed binary')

    def test_unknown_legacy_install(self):
        self.assertIsNone(xray_source.read_source(self.path))

    def test_persisted_source_survives_cache_clear(self):
        repo = 'https://github.com/Medium1992/Xray-core-fork'
        xray_source.record_source(self.path, repo, 'v26.11.9-h2Flow-7')
        xray_source._digest.cache_clear()
        self.assertEqual(xray_source.read_source(self.path), {'repository': repo, 'tag': 'v26.11.9-h2Flow-7'})

    def test_manual_replacement_invalidates_source(self):
        xray_source.record_source(self.path, 'https://github.com/a/b', 'v1')
        self.assertIsNotNone(xray_source.read_source(self.path))
        Path(self.path).write_bytes(b'different binary')
        self.assertIsNone(xray_source.read_source(self.path))

    def test_rollback_restores_previous_source(self):
        old = Path(self.path).read_bytes()
        xray_source.record_source(self.path, 'https://github.com/a/b', 'v1')
        Path(self.path).write_bytes(b'new binary that fails startup')
        self.assertIsNone(xray_source.read_source(self.path))
        Path(self.path).write_bytes(old)
        self.assertEqual(xray_source.read_source(self.path)['tag'], 'v1')

    def test_corrupt_metadata_is_unknown(self):
        for text in ['not json', '[]', '{"repository":"javascript:alert(1)"}']:
            Path(self.path + '.source.json').write_text(text)
            self.assertIsNone(xray_source.read_source(self.path))

    def test_failed_write_preserves_previous_metadata(self):
        xray_source.record_source(self.path, 'https://github.com/a/b', 'v1')
        with patch.object(xray_source.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                xray_source.record_source(self.path, 'https://github.com/c/d', 'v2')
        self.assertEqual(xray_source.read_source(self.path)['tag'], 'v1')
