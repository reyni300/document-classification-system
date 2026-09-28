import io
import logging
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
# Модули распознавания читают config.yaml из текущей папки
os.chdir(ROOT)


def _stub_missing_packages():
    try:
        import ollama  # noqa: F401
    except ImportError:
        stub = types.ModuleType('ollama')

        def chat(*_args, **_kwargs):
            raise ConnectionError('ollama недоступна в тестах')
        stub.chat = chat
        sys.modules['ollama'] = stub
    try:
        import deep_translator  # noqa: F401
    except ImportError:
        stub = types.ModuleType('deep_translator')

        class GoogleTranslator:
            def __init__(self, source='auto', target='ru'):
                pass

            def translate(self, text):
                return text
        stub.GoogleTranslator = GoogleTranslator
        sys.modules['deep_translator'] = stub


_stub_missing_packages()

# Ошибки разборов в тестах ожидаемы — их трассировки только мешают читать вывод
logging.getLogger('parser_service').setLevel(logging.CRITICAL)


def png_bytes(width=40, height=30, color=(255, 255, 255)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color).save(buf, 'PNG')
    return buf.getvalue()


def sample_result(parse_id='0' * 32):
    """Минимальный результат в формате сервиса — для тестов API и хранилища."""
    return {
        'schema_version': '1.0',
        'id': parse_id,
        'status': 'done',
        'source': {'filename': 'чек.png'},
        'pages': [{'number': 1, 'width': 40, 'height': 30,
                   'image_url': '/api/v1/parses/%s/pages/1/image' % parse_id,
                   'table_grid': None, 'words': []}],
        'documents': [],
        'summary': {'documents': 0, 'fields': 0, 'fields_located': 0,
                    'document_type': None, 'category': None, 'total': None, 'currency': None},
        'warnings': [],
        'logs': ['ok'],
    }


class FakePipeline:
    """Пайплайн без OCR и модели: кладёт картинку страницы и отдаёт результат."""

    def __init__(self, fail=None, gate=None):
        self.fail = fail
        self.gate = gate            # threading.Event — держит обработку, пока не отпустят
        self.calls = []

    def run(self, parse_id, source_path, filename, work_dir):
        self.calls.append((parse_id, source_path, filename, work_dir))
        if self.gate is not None:
            self.gate.wait(10)
        if self.fail:
            from parser_service.worker import ParseError
            raise ParseError(self.fail, logs=['ШАГ 1', 'упало'])
        with open(os.path.join(work_dir, 'page-1.png'), 'wb') as f:
            f.write(png_bytes())
        result = sample_result(parse_id)
        result['source']['filename'] = filename
        result['summary']['total'] = '258.00'
        return result
