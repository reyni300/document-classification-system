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

    def page(self, client, url):
        resp = client.get(url)
        self.assertEqual(resp.status_code, 200, url)
        text = resp.get_data(as_text=True)
        resp.close()
        return text

    def test_parse_page_starts_parsing_and_lists_history(self):
        client = self.make()
        for url in ('/', '/parse'):
            html = self.page(client, url)
            self.assertIn('ДокАнализ', html)
            self.assertIn("'/api/v1/parses'", html)          # отправка файла
            self.assertIn('historyBody', html)               # история разборов — здесь
            self.assertNotIn('annotationCanvas', html)       # документ здесь не рисуется сам —
            self.assertIn('<iframe id="viewerFrame"', html)  # просмотр открывается во фрейме
            self.assertIn('openIdDialog', html)              # «Просмотр» по ID/ссылке — тоже во фрейм

    def test_view_page_shows_one_parse(self):
        client = self.make()
        html = self.page(client, '/view')
        self.assertIn('annotationCanvas', html)              # подсветка
        self.assertIn('openParseById', html)                 # открывает разбор по ID
        self.assertNotIn('fileInput', html)                  # загрузки нет
        self.assertNotIn('historyBody', html)                # истории нет
        self.assertNotIn("method: 'POST'", html)             # разбор отсюда не запускается
        self.assertNotIn('<header>', html)                   # атомарный: без шапки и навигации сайта
        self.assertNotIn('href="/parse"', html)

    def test_embed_example_is_standalone(self):
        """Пример встраивания — чистый HTML/JS: ничего из приложения не подключает."""
        path = os.path.join(ROOT, 'embed-example', 'index.html')
        with open(path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('/view?id=', html)                     # открывает наш просмотр
        self.assertIn('<iframe id="frame"', html)            # во фрейме
        self.assertNotIn('<script src=', html)               # без внешних скриптов
        self.assertNotIn('common.js', html)
        self.assertNotIn('/api/v1', html)                    # API сам не трогает — только просмотр

    def test_view_tells_any_embedding_page_about_escape(self):
        html = self.page(self.make(), '/view')
        self.assertIn("postMessage({ source: 'dokanaliz-view', event: 'escape' }, '*')", html)

    def test_linked_values_shown_without_line_scraps(self):
        """Значения строки над документом — без обрывков линейки («(1 066)!»)."""
        html = self.page(self.make(), '/view')
        self.assertIn('function tidyNumber(text)', html)
        self.assertIn('const text = tidyNumber(', html)

    def test_viewer_switches_and_sticky_toolbars(self):
        html = self.page(self.make(), '/view')
        self.assertIn('state.showLinks = false;', html)          # «Связанные значения» выключены
        self.assertNotIn('dokanaliz.showLinks', html)            # и выбор не запоминается
        for btn in ('linksToggle', 'wordsToggle', 'btnToggleLeft', 'btnToggleLeftHighlight',
                    'btnToggleRight', 'btnToggleText'):
            self.assertRegex(html, r'class="vis-btn[^"]*switch[^"]*" id="%s"' % btn)
        self.assertIn('.preview-nav { position: sticky; top: 112px;', html)
        self.assertIn("if (side === 'right' && !_rightTextVisible) return [];", html)

    def test_link_to_block(self):
        html = self.page(self.make(), '/view')
        self.assertIn("get('block')", html)                      # /view?id=…&block=p1-b42
        self.assertIn('function copyBlockLink()', html)
        self.assertIn('function loadRaw(parseId)', html)          # блоки сырого JSON тоже
        self.assertIn('function toggleWordMode(on)', html)        # «По словам» — отдельный переключатель
        self.assertNotIn('function wordAt', html)                 # второго щелчка по блоку больше нет
        self.assertIn('const WORD_RE = /^p(\\d+)-b(\\d+)-w(\\d+)$/;', html)   # ссылка на слово
        with open(os.path.join(ROOT, 'embed-example', 'index.html'), encoding='utf-8') as f:
            self.assertIn("'&block='", f.read())                  # простая страница передаёт блок

    def test_old_links_redirect_to_view(self):
        client = self.make()
        resp = client.get('/?id=' + 'a' * 32)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('/view?id=' + 'a' * 32))

    def test_common_files_are_served(self):
        client = self.make()
        js = self.page(client, '/static/common.js')
        self.assertIn('function waitForParse', js)
        self.assertIn('--accent', self.page(client, '/static/common.css'))
        self.assertEqual(client.get('/static/../server.py').status_code, 404)

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
