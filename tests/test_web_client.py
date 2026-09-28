import io
import os
import sys
import unittest

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from web_client.server import create_app  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, content=b'{}', headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {'Content-Type': 'application/json'}


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


class WebClientTest(unittest.TestCase):
    def make(self, **kwargs):
        self.session = FakeSession(**kwargs)
        return create_app('http://parser:8000/', session=self.session).test_client()

    def test_serves_page(self):
        client = self.make()
        resp = client.get('/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('ДокАнализ'.encode('utf-8'), resp.data)
        self.assertIn(b'/api/v1/parses', resp.data)       # страница говорит с новым API
        self.assertNotIn(b"'/api/process'", resp.data)
        resp.close()

    def test_forwards_upload_to_parser(self):
        client = self.make(response=FakeResponse(
            202, b'{"id": "abc"}',
            {'Content-Type': 'application/json', 'Location': '/api/v1/parses/abc',
             'Transfer-Encoding': 'chunked'}))
        resp = client.post('/api/v1/parses',
                           data={'file': (io.BytesIO(b'%PDF'), 'баланс.pdf')},
                           content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.get_json(), {'id': 'abc'})
        self.assertEqual(resp.headers['Location'], '/api/v1/parses/abc')
        self.assertNotIn('Transfer-Encoding', resp.headers)

        method, url, kwargs = self.session.calls[0]
        self.assertEqual((method, url), ('POST', 'http://parser:8000/api/v1/parses'))
        self.assertIn(b'%PDF', kwargs['data'])
        self.assertTrue(kwargs['headers']['Content-Type'].startswith('multipart/form-data'))
        self.assertNotIn('Host', kwargs['headers'])

    def test_forwards_query_status_and_binary(self):
        png = b'\x89PNG\r\n\x1a\nxxx'
        client = self.make(response=FakeResponse(200, png, {'Content-Type': 'image/png'}))
        resp = client.get('/api/v1/parses/abc/pages/1/image?x=1')
        self.assertEqual(resp.data, png)
        self.assertEqual(resp.mimetype, 'image/png')
        _, url, kwargs = self.session.calls[0]
        self.assertEqual(url, 'http://parser:8000/api/v1/parses/abc/pages/1/image')
        self.assertEqual(kwargs['params'].to_dict(), {'x': '1'})

    def test_passes_errors_through(self):
        client = self.make(response=FakeResponse(404, b'{"error": {"code": "not_found"}}'))
        resp = client.delete('/api/v1/parses/abc')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.session.calls[0][0], 'DELETE')

    def test_parser_down_is_502(self):
        client = self.make(error=requests.ConnectionError('refused'))
        resp = client.get('/api/v1/parses')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.get_json()['error']['code'], 'parser_unavailable')

    def test_client_does_not_import_parsing_modules(self):
        """Клиент независим от слоя разбора: ни OCR, ни модели, ни правил."""
        import ast
        import os
        folder = os.path.join(ROOT, 'web_client')
        forbidden = {'ocr', 'llm', 'orchestrator', 'rule_fields', 'agents',
                     'preprocessing', 'translation', 'parser_service', 'db'}
        for name in os.listdir(folder):
            if not name.endswith('.py'):
                continue
            with open(os.path.join(folder, name), encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = {a.name.split('.')[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    roots = {(node.module or '').split('.')[0]}
                else:
                    continue
                self.assertFalse(roots & forbidden, '%s импортирует %s' % (name, roots & forbidden))


if __name__ == '__main__':
    unittest.main()
