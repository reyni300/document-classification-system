import json
import os
import shutil
import tempfile
import unittest

from tests.helpers import sample_result  # noqa: F401  (настраивает пути)
from parser_service import storage
from parser_service.storage import ParseNotFound, ParseStore


def writer(data=b'%PDF-1.4 test'):
    def save(path):
        with open(path, 'wb') as f:
            f.write(data)
    return save


class ParseStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ParseStore(os.path.join(self.tmp, 'parses'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_saves_source_and_queued_status(self):
        pid = self.store.create('Баланс.PDF', writer(b'abc'))
        self.assertTrue(storage.is_valid_id(pid))
        status = self.store.get_status(pid)
        self.assertEqual(status['status'], storage.QUEUED)
        self.assertEqual(status['filename'], 'Баланс.PDF')
        self.assertEqual(status['source_file'], 'source.pdf')
        self.assertEqual(status['size_bytes'], 3)
        with open(self.store.source_path(pid), 'rb') as f:
            self.assertEqual(f.read(), b'abc')

    def test_ids_are_unique(self):
        ids = {self.store.create('a.png', writer()) for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_unsupported_format_leaves_nothing(self):
        with self.assertRaises(ValueError):
            self.store.create('скрипт.exe', writer())
        self.assertEqual(os.listdir(self.store.root), [])

    def test_failed_save_removes_folder(self):
        def broken(_path):
            raise IOError('диск кончился')
        with self.assertRaises(IOError):
            self.store.create('a.pdf', broken)
        self.assertEqual(os.listdir(self.store.root), [])

    def test_path_traversal_is_not_found(self):
        for bad in ('../../etc/passwd', '..', '', 'ABC', 'g' * 32, '0' * 31):
            with self.assertRaises(ParseNotFound):
                self.store.get_status(bad)
            self.assertFalse(self.store.exists(bad))

    def test_update_status_and_result(self):
        pid = self.store.create('a.png', writer())
        self.assertIsNone(self.store.get_result(pid))
        self.store.update_status(pid, status=storage.DONE, summary={'total': '1'})
        self.store.save_result(pid, {'id': pid, 'текст': 'кириллица'})
        self.assertEqual(self.store.get_status(pid)['summary'], {'total': '1'})
        self.assertEqual(self.store.get_result(pid)['текст'], 'кириллица')
        # результат пишется человекочитаемым UTF-8, без \u-экранирования
        with open(self.store.result_path(pid), encoding='utf-8') as f:
            self.assertIn('кириллица', f.read())

    def test_no_tmp_files_left_after_write(self):
        pid = self.store.create('a.png', writer())
        self.store.save_result(pid, {'x': 1})
        self.assertFalse([n for n in os.listdir(self.store.work_dir(pid)) if n.endswith('.tmp')])

    def test_list_newest_first_and_skips_broken(self):
        first = self.store.create('1.png', writer())
        second = self.store.create('2.png', writer())
        self.store.update_status(first, created_at='2026-01-01T00:00:00+00:00')
        self.store.update_status(second, created_at='2026-02-01T00:00:00+00:00')
        os.makedirs(os.path.join(self.store.root, 'мусор'))
        broken = self.store.create('3.png', writer())
        with open(os.path.join(self.store.work_dir(broken), 'status.json'), 'w') as f:
            f.write('{не json')
        self.assertEqual([s['id'] for s in self.store.list()], [second, first])
        self.assertEqual(len(self.store.list(limit=1)), 1)

    def test_delete(self):
        pid = self.store.create('a.png', writer())
        self.store.delete(pid)
        self.assertFalse(self.store.exists(pid))
        with self.assertRaises(ParseNotFound):
            self.store.delete(pid)

    def test_page_image_path(self):
        pid = self.store.create('a.pdf', writer())
        self.assertIsNone(self.store.page_image_path(pid, 1))
        open(os.path.join(self.store.work_dir(pid), 'page-1.png'), 'wb').close()
        self.assertTrue(self.store.page_image_path(pid, 1).endswith('page-1.png'))

    def test_status_file_is_valid_json_on_disk(self):
        pid = self.store.create('a.jpg', writer())
        with open(os.path.join(self.store.work_dir(pid), 'status.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['id'], pid)


if __name__ == '__main__':
    unittest.main()
