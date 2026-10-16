import json
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp_root = ROOT / '.operator/tests'
        temp_root.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(dir=temp_root)
        cls.repo = Path(cls.temp.name)
        shutil.copytree(ROOT / 'tools', cls.repo / 'tools')
        for name in ('gunpla_dictionary.json', 'dictionary_manifest.json'):
            shutil.copyfile(ROOT / name, cls.repo / name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); cls.port = sock.getsockname()[1]
        cls.base = 'http://127.0.0.1:' + str(cls.port)
        cls.process = subprocess.Popen([sys.executable, str(cls.repo / 'tools/dictionary_ops.py'), 'serve', '--repo', str(cls.repo), '--port', str(cls.port), '--no-browser'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'})
        line = cls.process.stdout.readline()
        if '/#' not in line: raise RuntimeError('Server failed to start')
        cls.token = line.split('/#', 1)[1].strip()

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate(); cls.process.communicate(timeout=10); cls.temp.cleanup()

    def call(self, path, value=None, *, token=True, origin=True, host=None):
        headers = {}
        if token: headers['X-Operator-Token'] = self.token
        if origin: headers['Origin'] = self.base
        if host: headers['Host'] = host
        data = json.dumps(value).encode() if value is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def test_ui_has_csp_and_no_embedded_token(self):
        status, data, headers = self.call('/')
        self.assertEqual(200, status); self.assertNotIn(self.token.encode(), data)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_api_requires_secret(self):
        self.assertEqual(403, self.call('/api/state', token=False)[0])

    def test_cross_site_post_is_rejected(self):
        self.assertEqual(403, self.call('/api/collect', {}, origin=False)[0])

    def test_dns_rebinding_host_is_rejected(self):
        self.assertEqual(403, self.call('/api/state', host='attacker.example')[0])

    def test_state_matches_catalog(self):
        status, data, _ = self.call('/api/state')
        self.assertEqual(200, status); self.assertEqual(len(json.loads((self.repo / 'gunpla_dictionary.json').read_bytes())['products']), json.loads(data)['products'])

    def test_import_cannot_replace_base_dictionary(self):
        before = (self.repo / 'gunpla_dictionary.json').read_bytes()
        self.assertEqual(400, self.call('/api/import', {'schema_version': 1, 'items': [], 'base_sha256': 'wrong'})[0])
        self.assertEqual(before, (self.repo / 'gunpla_dictionary.json').read_bytes())

    def test_static_traversal_is_rejected(self):
        self.assertEqual(404, self.call('/../gunpla_dictionary.json')[0])

if __name__ == '__main__': unittest.main()
