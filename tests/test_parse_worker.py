import os
import shutil
import tempfile
import threading
import time
import unittest

from tests.helpers import FakePipeline
from parser_service import storage
from parser_service.storage import ParseStore
from parser_service.worker import ParseWorker


def save(path):
    with open(path, 'wb') as f:
        f.write(b'x')


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ParseWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ParseStore(os.path.join(self.tmp, 'parses'))
        self.workers = []

    def tearDown(self):
        for w in self.workers:
            w.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, pipeline):
        worker = ParseWorker(self.store, lambda: pipeline)
        worker.start()
        self.workers.append(worker)
        return worker

    def status(self, pid):
        return self.store.get_status(pid)['status']

    def test_success_stores_result_and_summary(self):
        pipeline = FakePipeline()
        worker = self.start(pipeline)
        pid = self.store.create('чек.png', save)
        worker.submit(pid)
        self.assertTrue(wait_for(lambda: self.status(pid) == storage.DONE))

        status = self.store.get_status(pid)
        self.assertEqual(status['summary']['total'], '258.00')
        self.assertIsNotNone(status['started_at'])
        self.assertIsNotNone(status['finished_at'])
        result = self.store.get_result(pid)
        self.assertEqual(result['source']['filename'], 'чек.png')
        self.assertIn('duration_sec', result['timing'])
        # пайплайн получил исходник и папку именно этого разбора
        _, source, filename, work_dir = pipeline.calls[0]
        self.assertEqual(source, self.store.source_path(pid))
        self.assertEqual(work_dir, self.store.work_dir(pid))

    def test_failure_is_reported_with_logs(self):
        worker = self.start(FakePipeline(fail='Не удалось извлечь данные'))
        pid = self.store.create('a.pdf', save)
        worker.submit(pid)
        self.assertTrue(wait_for(lambda: self.status(pid) == storage.FAILED))
        status = self.store.get_status(pid)
        self.assertEqual(status['error'], 'Не удалось извлечь данные')
        self.assertEqual(status['logs'], ['ШАГ 1', 'упало'])
        self.assertIsNone(self.store.get_result(pid))

    def test_one_at_a_time_and_queue_position(self):
        gate = threading.Event()
        worker = self.start(FakePipeline(gate=gate))
        first = self.store.create('1.png', save)
        second = self.store.create('2.png', save)
        third = self.store.create('3.png', save)
        for pid in (first, second, third):
            worker.submit(pid)

        self.assertTrue(wait_for(lambda: self.status(first) == storage.PROCESSING))
        self.assertEqual(self.status(second), storage.QUEUED)
        self.assertEqual(worker.current, first)
        self.assertEqual(worker.queue_position(second), 1)
        self.assertEqual(worker.queue_position(third), 2)
        self.assertIsNone(worker.queue_position(first))

        gate.set()
        self.assertTrue(wait_for(lambda: self.status(third) == storage.DONE))
        self.assertEqual(worker.queue_length(), 0)

    def test_recovers_unfinished_after_restart(self):
        queued = self.store.create('1.png', save)
        interrupted = self.store.create('2.png', save)
        self.store.update_status(interrupted, status=storage.PROCESSING,
                                 started_at=storage.now_iso())
        done = self.store.create('3.png', save)
        self.store.update_status(done, status=storage.DONE)

        pipeline = FakePipeline()
        self.start(pipeline)
        self.assertTrue(wait_for(lambda: self.status(queued) == storage.DONE
                                 and self.status(interrupted) == storage.DONE))
        processed = {call[0] for call in pipeline.calls}
        self.assertEqual(processed, {queued, interrupted})     # готовый не трогаем

    def test_deleted_while_queued_is_skipped(self):
        gate = threading.Event()
        pipeline = FakePipeline(gate=gate)
        worker = self.start(pipeline)
        first = self.store.create('1.png', save)
        second = self.store.create('2.png', save)
        worker.submit(first)
        worker.submit(second)
        self.assertTrue(wait_for(lambda: self.status(first) == storage.PROCESSING))
        self.store.delete(second)
        gate.set()
        self.assertTrue(wait_for(lambda: worker.queue_length() == 0 and worker.current is None))
        self.assertEqual([c[0] for c in pipeline.calls], [first])

    def test_pipeline_created_lazily_once(self):
        made = []

        def factory():
            made.append(1)
            return FakePipeline()
        worker = ParseWorker(self.store, factory)
        worker.start()
        self.workers.append(worker)
        self.assertEqual(made, [])                 # API стартует, не дожидаясь OCR и модели
        for name in ('1.png', '2.png'):
            pid = self.store.create(name, save)
            worker.submit(pid)
        self.assertTrue(wait_for(lambda: all(s['status'] == storage.DONE
                                             for s in self.store.list())))
        self.assertEqual(made, [1])


if __name__ == '__main__':
    unittest.main()
