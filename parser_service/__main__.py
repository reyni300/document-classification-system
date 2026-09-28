"""
Запуск сервиса разбора:  python -m parser_service

Настройки — переменными окружения:
    PARSER_HOST        адрес (по умолчанию 0.0.0.0)
    PARSER_PORT        порт (по умолчанию 8000)
    PARSER_DATA_DIR    где хранить разборы (по умолчанию data/parses)
    PARSER_MAX_UPLOAD_MB  предел размера файла (по умолчанию 25)
    OLLAMA_HOST        адрес Ollama (по умолчанию http://localhost:11434)
"""

import logging
import os
import sys

# Модули распознавания читают config.yaml из текущей папки и импортируются
# от корня репозитория — запускаемся оттуда, откуда бы нас ни вызвали.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from parser_service import __version__                    # noqa: E402
from parser_service.api import create_app                 # noqa: E402
from parser_service.storage import ParseStore             # noqa: E402
from parser_service.worker import ParseWorker             # noqa: E402


def make_pipeline():
    from parser_service.pipeline import DocumentPipeline
    return DocumentPipeline()


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    host = os.environ.get('PARSER_HOST', '0.0.0.0')
    port = int(os.environ.get('PARSER_PORT', '8000'))
    data_dir = os.environ.get('PARSER_DATA_DIR', os.path.join('data', 'parses'))
    max_mb = int(os.environ.get('PARSER_MAX_UPLOAD_MB', '25'))

    store = ParseStore(data_dir)
    worker = ParseWorker(store, make_pipeline)
    worker.start()
    app = create_app(store, worker, max_upload_mb=max_mb)

    print('=' * 50)
    print('  ДокАнализ — сервис разбора %s' % __version__)
    print('  API:       http://localhost:%d/api/v1' % port)
    print('  Разборы:   %s' % os.path.abspath(data_dir))
    print('=' * 50)
    # Без debug и без перезагрузчика: перезагрузчик запускает второй процесс,
    # а с ним — второй обработчик очереди, и два разбора пошли бы параллельно.
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == '__main__':
    main()
