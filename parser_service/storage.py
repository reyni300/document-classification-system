"""
Хранилище разборов на диске.

Каждый разбор — отдельная папка data/parses/<id>/:

    source.<ext>       исходный файл, как его прислали
    status.json        состояние разбора (очередь, обработка, готово, ошибка)
    result.json        результат — появляется, только когда разбор готов
    page-<n>.png       картинки страниц; координаты полей относятся к ним

Файлы JSON пишутся атомарно (во временный файл и переименованием), поэтому
читающий никогда не увидит наполовину записанный результат, даже если
обработка упадёт посреди записи.
"""

import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone


QUEUED = 'queued'
PROCESSING = 'processing'
DONE = 'done'
FAILED = 'failed'
STATUSES = (QUEUED, PROCESSING, DONE, FAILED)

ALLOWED_EXTENSIONS = ('pdf', 'png', 'jpg', 'jpeg')

# ID — 32 шестнадцатеричных символа (uuid4). Проверяем формат до того, как
# подставить его в путь: иначе запрос с ID вида "../../etc" вышел бы за
# пределы папки с разборами.
_ID_RE = re.compile(r'^[0-9a-f]{32}$')


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def is_valid_id(parse_id):
    return bool(parse_id) and bool(_ID_RE.match(str(parse_id)))


def extension_of(filename):
    if not filename or '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[1].lower()


class ParseNotFound(KeyError):
    pass


class ParseStore:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    # --- пути ----------------------------------------------------------------
    def _dir(self, parse_id):
        if not is_valid_id(parse_id):
            raise ParseNotFound(parse_id)
        return os.path.join(self.root, parse_id)

    def work_dir(self, parse_id):
        return self._dir(parse_id)

    def exists(self, parse_id):
        return is_valid_id(parse_id) and os.path.isfile(
            os.path.join(self.root, parse_id, 'status.json'))

    def source_path(self, parse_id):
        status = self.get_status(parse_id)
        return os.path.join(self._dir(parse_id), status['source_file'])

    def page_image_path(self, parse_id, page_number):
        path = os.path.join(self._dir(parse_id), 'page-%d.png' % int(page_number))
        return path if os.path.isfile(path) else None

    # --- запись/чтение JSON --------------------------------------------------
    @staticmethod
    def _write_json(path, data):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    # --- операции ------------------------------------------------------------
    def create(self, filename, save_source, size_bytes=None):
        """
        Заводит разбор и сохраняет исходный файл.

        save_source(path) — функция, которая пишет файл по указанному пути
        (у Flask это FileStorage.save). Так хранилище не зависит от Flask.
        """
        ext = extension_of(filename)
        if ext not in ALLOWED_EXTENSIONS:
            raise ValueError('Формат не поддерживается: %s' % filename)

        parse_id = uuid.uuid4().hex
        folder = os.path.join(self.root, parse_id)
        os.makedirs(folder)
        source_file = 'source.' + ext
        source_path = os.path.join(folder, source_file)
        try:
            save_source(source_path)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        status = {
            'id': parse_id,
            'status': QUEUED,
            'filename': filename,
            'source_file': source_file,
            'size_bytes': size_bytes if size_bytes is not None else os.path.getsize(source_path),
            'created_at': now_iso(),
            'started_at': None,
            'finished_at': None,
            'error': None,
            'summary': None,
        }
        with self._lock:
            self._write_json(os.path.join(folder, 'status.json'), status)
        return parse_id

    def get_status(self, parse_id):
        path = os.path.join(self._dir(parse_id), 'status.json')
        if not os.path.isfile(path):
            raise ParseNotFound(parse_id)
        return self._read_json(path)

    def update_status(self, parse_id, **changes):
        with self._lock:
            status = self.get_status(parse_id)
            status.update(changes)
            self._write_json(os.path.join(self._dir(parse_id), 'status.json'), status)
            return status

    def save_result(self, parse_id, result):
        with self._lock:
            self._write_json(os.path.join(self._dir(parse_id), 'result.json'), result)

    def get_result(self, parse_id):
        """Результат разбора или None, если его ещё (или уже) нет."""
        path = os.path.join(self._dir(parse_id), 'result.json')
        if not os.path.isfile(path):
            if not self.exists(parse_id):
                raise ParseNotFound(parse_id)
            return None
        return self._read_json(path)

    def result_path(self, parse_id):
        path = os.path.join(self._dir(parse_id), 'result.json')
        return path if os.path.isfile(path) else None

    def list(self, limit=None):
        """Все разборы, новые сверху."""
        items = []
        for name in os.listdir(self.root):
            if not self.exists(name):
                continue
            try:
                items.append(self.get_status(name))
            except (OSError, ValueError, ParseNotFound):
                continue                 # битую папку просто пропускаем
        items.sort(key=lambda s: s.get('created_at') or '', reverse=True)
        return items[:limit] if limit else items

    def delete(self, parse_id):
        folder = self._dir(parse_id)
        if not os.path.isdir(folder):
            raise ParseNotFound(parse_id)
        with self._lock:
            shutil.rmtree(folder)
