"""
Очередь разборов и фоновый обработчик.

Одна страница разбирается 15-30 секунд, поэтому API не держит соединение,
пока документ обрабатывается: оно сразу отдаёт ID, а работа идёт здесь.

Обработчик ровно один. На машине с 5.6 ГБ памяти два разбора одновременно
означают две копии модели в памяти — система снимет Ollama, и оба разбора
упадут. Остальные документы ждут своей очереди.
"""

import logging
import queue
import threading
import time
import traceback

from . import storage


log = logging.getLogger('parser_service.worker')


class ParseError(Exception):
    """Разбор не удался; logs — журнал шагов до места ошибки."""

    def __init__(self, message, logs=None):
        super().__init__(message)
        self.logs = logs or []


class ParseWorker:
    def __init__(self, store, pipeline_factory):
        """
        pipeline_factory() -> объект с методом run(parse_id, source_path,
        filename, work_dir) -> dict результата. Создаётся лениво, в потоке
        обработчика: загрузка OCR и модели не должна задерживать старт API.
        """
        self.store = store
        self._pipeline_factory = pipeline_factory
        self._pipeline = None
        self._queue = queue.Queue()
        self._pending = []                 # ID в порядке очереди — для queue_position
        self._pending_lock = threading.Lock()
        self._current = None
        self._thread = None
        self._stop = threading.Event()

    # --- управление ----------------------------------------------------------
    def start(self):
        self._recover()
        self._thread = threading.Thread(target=self._loop, name='parse-worker', daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout)

    def submit(self, parse_id):
        with self._pending_lock:
            self._pending.append(parse_id)
        self._queue.put(parse_id)

    def queue_position(self, parse_id):
        """1 — следующий на обработку; None — не в очереди."""
        with self._pending_lock:
            if parse_id in self._pending:
                return self._pending.index(parse_id) + 1
        return None

    def queue_length(self):
        with self._pending_lock:
            return len(self._pending)

    @property
    def current(self):
        return self._current

    def _recover(self):
        """
        После перезапуска сервиса подхватываем незаконченные разборы.
        «processing» тоже возвращаем в очередь: процесс, который его вёл,
        уже не существует, и сам разбор никогда не закончится.
        """
        unfinished = [s for s in self.store.list()
                      if s.get('status') in (storage.QUEUED, storage.PROCESSING)]
        for status in sorted(unfinished, key=lambda s: s.get('created_at') or ''):
            if status['status'] == storage.PROCESSING:
                self.store.update_status(status['id'], status=storage.QUEUED, started_at=None)
            log.info('Разбор %s возвращён в очередь после перезапуска', status['id'])
            self.submit(status['id'])

    # --- обработка -----------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            parse_id = self._queue.get()
            if parse_id is None:
                break
            with self._pending_lock:
                if parse_id in self._pending:
                    self._pending.remove(parse_id)
            if not self.store.exists(parse_id):
                continue                  # удалили, пока ждал в очереди
            self._current = parse_id
            try:
                self.process(parse_id)
            finally:
                self._current = None

    def process(self, parse_id):
        started = time.monotonic()
        self.store.update_status(parse_id, status=storage.PROCESSING,
                                 started_at=storage.now_iso(), error=None)
        status = self.store.get_status(parse_id)
        try:
            if self._pipeline is None:
                self._pipeline = self._pipeline_factory()
            result = self._pipeline.run(
                parse_id=parse_id,
                source_path=self.store.source_path(parse_id),
                filename=status['filename'],
                work_dir=self.store.work_dir(parse_id),
            )
            finished = storage.now_iso()
            result.setdefault('timing', {})
            result['timing'].update({
                'created_at': status['created_at'],
                'started_at': self.store.get_status(parse_id)['started_at'],
                'finished_at': finished,
                'duration_sec': round(time.monotonic() - started, 1),
            })
            if not self.store.exists(parse_id):
                return                    # удалили во время обработки
            self.store.save_result(parse_id, result)
            self.store.update_status(parse_id, status=storage.DONE, finished_at=finished,
                                     summary=result.get('summary'))
            log.info('Разбор %s готов за %.1f с', parse_id, time.monotonic() - started)
        except Exception as exc:
            logs = getattr(exc, 'logs', None) or []
            log.error('Разбор %s не удался: %s\n%s', parse_id, exc, traceback.format_exc())
            if self.store.exists(parse_id):
                self.store.update_status(parse_id, status=storage.FAILED,
                                         finished_at=storage.now_iso(),
                                         error=str(exc) or exc.__class__.__name__,
                                         logs=logs)
