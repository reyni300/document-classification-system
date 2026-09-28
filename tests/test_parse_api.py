import io
import os
import shutil
import tempfile
import threading
import time
import unittest

from tests.helpers import FakePipeline, png_bytes
from parser_service import storage
from parser_service.api import create_app
from parser_service.storage import ParseStore
from parser_service.worker import ParseWorker


class ParseApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ParseStore(os.path.join(self.tmp, 'parses'))
        self.gate = threading.Event()
        self.gate.set()
        self.pipeline = FakePipeline(gate=self.gate)
        self.worker = ParseWorker(self.store, lambda: self.pipeline)
        self.worker.start()
        self.app = create_app(self.store, self.worker, max_upload_mb=1)
        self.client = self.app.test_client()

    def tearDown(self):
        self.gate.set()
        self.worker.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- помощники -----------------------------------------------------------
    def upload(self, name='чек.png', data=None):
        data = png_bytes() if data is None else data
        return self.client.post('/api/v1/parses',
                                data={'file': (io.BytesIO(data), name)},
                                content_type='multipart/form-data')

    def wait_status(self, pid, wanted, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            body = self.client.get('/api/v1/parses/' + pid).get_json()
            if body['status'] == wanted:
                return body
            time.sleep(0.02)
        self.fail('разбор %s не дошёл до %s' % (pid, wanted))

    # --- приём файла ---------------------------------------------------------
    def test_upload_returns_id_immediately(self):
        self.gate.clear()                          # обработка «висит»
        resp = self.upload()
        self.assertEqual(resp.status_code, 202)
        body = resp.get_json()
        self.assertTrue(storage.is_valid_id(body['id']))
        self.assertIn(body['status'], (storage.QUEUED, storage.PROCESSING))
        self.assertEqual(body['filename'], 'чек.png')
        self.assertEqual(resp.headers['Location'], '/api/v1/parses/' + body['id'])
        self.assertEqual(body['links']['result'], '/api/v1/parses/%s/result' % body['id'])
        self.assertNotIn('result', body)

    def test_upload_without_file(self):
        resp = self.client.post('/api/v1/parses', data={}, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error']['code'], 'file_missing')

    def test_upload_unsupported_format(self):
        resp = self.upload('документ.docx', b'PK')
        self.assertEqual(resp.status_code, 415)
        error = resp.get_json()['error']
        self.assertEqual(error['code'], 'unsupported_format')
        self.assertIn('pdf', error['allowed'])
        self.assertEqual(self.store.list(), [])

    def test_upload_too_large(self):
        resp = self.upload('big.pdf', b'0' * (2 * 1024 * 1024))
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.get_json()['error']['code'], 'file_too_large')

    # --- статус и результат --------------------------------------------------
    def test_full_cycle_by_id(self):
        pid = self.upload().get_json()['id']
        body = self.wait_status(pid, storage.DONE)
        self.assertEqual(body['result']['id'], pid)
        self.assertEqual(body['summary']['total'], '258.00')
        self.assertIsNone(body['queue_position'])

        result = self.client.get('/api/v1/parses/%s/result' % pid)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.mimetype, 'application/json')
        self.assertEqual(result.get_json()['schema_version'], '1.0')

        image = self.client.get('/api/v1/parses/%s/pages/1/image' % pid)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.mimetype, 'image/png')
        self.assertEqual(image.data[:8], b'\x89PNG\r\n\x1a\n')
        image.close()
        result.close()

    def test_result_not_ready_is_409(self):
        self.gate.clear()
        pid = self.upload().get_json()['id']
        resp = self.client.get('/api/v1/parses/%s/result' % pid)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error']['code'], 'not_ready')

    def test_queue_position_is_visible(self):
        self.gate.clear()
        first = self.upload('1.png').get_json()['id']
        self.wait_status(first, storage.PROCESSING)
        second = self.upload('2.png').get_json()
        self.assertEqual(second['status'], storage.QUEUED)
        self.assertEqual(second['queue_position'], 1)

    def test_failed_parse_reports_error_and_logs(self):
        self.pipeline.fail = 'Не удалось извлечь данные из документа'
        pid = self.upload().get_json()['id']
        body = self.wait_status(pid, storage.FAILED)
        self.assertEqual(body['error'], 'Не удалось извлечь данные из документа')
        self.assertEqual(body['logs'], ['ШАГ 1', 'упало'])
        self.assertNotIn('result', body)

    def test_unknown_and_malformed_ids_are_404(self):
        for pid in ('0' * 32, '..%2F..%2Fetc', 'abc'):
            resp = self.client.get('/api/v1/parses/' + pid)
            self.assertEqual(resp.status_code, 404, pid)
            self.assertEqual(resp.get_json()['error']['code'], 'not_found')
        self.assertEqual(self.client.get('/api/v1/parses/%s/result' % ('0' * 32)).status_code, 404)

    def test_missing_page_image_is_404(self):
        pid = self.upload().get_json()['id']
        self.wait_status(pid, storage.DONE)
        resp = self.client.get('/api/v1/parses/%s/pages/7/image' % pid)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()['error']['code'], 'page_not_found')

    # --- список и удаление ---------------------------------------------------
    def test_list(self):
        a = self.upload('a.png').get_json()['id']
        b = self.upload('b.png').get_json()['id']
        self.wait_status(b, storage.DONE)
        body = self.client.get('/api/v1/parses').get_json()
        self.assertEqual(body['count'], 2)
        self.assertEqual({i['id'] for i in body['items']}, {a, b})
        self.assertTrue(all('result' not in i for i in body['items']))   # список лёгкий

    def test_delete(self):
        pid = self.upload().get_json()['id']
        self.wait_status(pid, storage.DONE)
        self.assertEqual(self.client.delete('/api/v1/parses/' + pid).status_code, 204)
        self.assertEqual(self.client.get('/api/v1/parses/' + pid).status_code, 404)

    def test_delete_while_processing_is_409(self):
        self.gate.clear()
        pid = self.upload().get_json()['id']
        self.wait_status(pid, storage.PROCESSING)
        resp = self.client.delete('/api/v1/parses/' + pid)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()['error']['code'], 'busy')

    # --- служебное -----------------------------------------------------------
    def test_health(self):
        body = self.client.get('/api/v1/health').get_json()
        self.assertEqual(body['status'], 'ok')
        self.assertIn('queue_length', body)
        self.assertIn('reachable', body['llm'])        # Ollama в тестах нет — но ответ есть

    def test_unknown_route_is_json_404(self):
        resp = self.client.get('/api/v1/nothing')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())

    def test_cyrillic_is_not_escaped_and_cors_is_open(self):
        resp = self.upload('Баланс.png')
        self.assertIn('Баланс.png'.encode('utf-8'), resp.data)
        self.assertEqual(resp.headers['Access-Control-Allow-Origin'], '*')


if __name__ == '__main__':
    unittest.main()
