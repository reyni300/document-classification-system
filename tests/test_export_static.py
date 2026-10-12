"""
Выгрузка разборов в папку для GitHub Pages: tools/export_static.py.

Сервис подменяется: проверяем, что получается в папке и что страницы
переключены в режим «разбор из файлов рядом».
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import export_static  # noqa: E402
from PIL import Image  # noqa: E402
import io  # noqa: E402


def page_png(size=(1240, 1754)):
    buf = io.BytesIO()
    Image.new('RGB', size, 'white').save(buf, 'PNG')
    return buf.getvalue()

ID = 'f944617fb97b472fb0f3e3b994881627'
RESULT = {'id': ID, 'source': {'filename': 'opu-2025.pdf'},
          'pages': [{'number': 1, 'image_url': '/api/v1/parses/%s/pages/1/image' % ID},
                    {'number': 2, 'image_url': '/api/v1/parses/%s/pages/2/image' % ID}]}


def fake_fetch(service, path):
    if path.endswith('/result'):
        return json.dumps(RESULT).encode()
    if path.endswith('/raw'):
        return b'{"pages": []}'
    if '/pages/' in path:
        return page_png()
    raise RuntimeError("404")


class ExportStaticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, 'site')
        self.files = os.path.join(self.tmp.name, 'orig')
        os.makedirs(self.files)
        with open(os.path.join(self.files, 'opu-2025.pdf'), 'wb') as f:
            f.write(b'%PDF')
        self.csv = os.path.join(self.tmp.name, 'list.csv')
        with open(self.csv, 'w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['year', 'quarter', 'document', 'id'])
            w.writerow(['2025', 'Q4', 'Отчёт о прибылях и убытках', ID])

    def tearDown(self):
        self.tmp.cleanup()

    def run_export(self):
        argv = ['export_static.py', self.csv, '--files', self.files, '--out', self.out]
        with mock.patch.object(export_static, 'fetch', fake_fetch), \
                mock.patch.object(sys, 'argv', argv), mock.patch('builtins.print'):
            export_static.main()

    def read(self, *parts):
        with open(os.path.join(self.out, *parts), encoding='utf-8') as f:
            return f.read()

    def test_json_named_by_id_and_kept_as_is(self):
        self.run_export()
        self.assertEqual(json.loads(self.read(ID + '.json')), RESULT)
        self.assertIn('\n  "pages"', self.read(ID + '.json'))      # с отступами, не одной строкой
        self.assertIn('\n', self.read('raw', ID + '.json'))
        self.assertTrue(os.path.isfile(os.path.join(self.out, 'raw', ID + '.json')))
        for n in (1, 2):
            jpg = os.path.join(self.out, 'pages', ID, 'page-%d.jpg' % n)
            with Image.open(jpg) as img:                      # JPEG того же размера,
                self.assertEqual(img.format, 'JPEG')          # координаты не съезжают
                self.assertEqual(img.size, (1240, 1754))
        self.assertTrue(os.path.isfile(os.path.join(self.out, 'files', 'opu-2025.pdf')))

    def test_pages_read_parses_from_their_folder(self):
        self.run_export()
        view, index = self.read('view.html'), self.read('index.html')
        self.assertIn('const STATIC_DATA = true;', view)
        self.assertIn("const STATIC_IMAGE_EXT = 'jpg';", view)
        self.assertIn('var STATIC_DATA = true;', index)
        self.assertIn('href="static/common.css"', view)          # без /static/ от корня сайта
        self.assertNotIn('"/static/', view)
        self.assertTrue(os.path.isfile(os.path.join(self.out, 'static', 'common.js')))

    def test_png_on_request(self):
        argv = ['export_static.py', self.csv, '--out', self.out, '--images', 'png']
        with mock.patch.object(export_static, 'fetch', fake_fetch), \
                mock.patch.object(sys, 'argv', argv), mock.patch('builtins.print'):
            export_static.main()
        self.assertTrue(os.path.isfile(os.path.join(self.out, 'pages', ID, 'page-1.png')))
        self.assertIn("const STATIC_IMAGE_EXT = 'png';", self.read('view.html'))

    def test_board_rows_with_links(self):
        self.run_export()
        rows = [line.split('\t') for line in self.read('board.tsv').splitlines()]
        self.assertEqual(rows[0], ['year', 'quarter', 'document', 'file', 'json', 'preview'])
        site = 'https://reyni300.github.io/dokanaliz-demo/'
        self.assertEqual(rows[1], ['2025', 'Q4', 'Отчёт о прибылях и убытках',
                                   site + 'files/opu-2025.pdf', site + ID + '.json',
                                   site + '?id=' + ID])

    def test_service_pages_stay_on_api(self):
        """В самом приложении режим по-прежнему «у сервиса»."""
        for path in (('web_client', 'static', 'view.html'), ('embed-example', 'index.html')):
            with open(os.path.join(ROOT, *path), encoding='utf-8') as f:
                self.assertIn('STATIC_DATA = false;', f.read())


if __name__ == '__main__':
    unittest.main()
