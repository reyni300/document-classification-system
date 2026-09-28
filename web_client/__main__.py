"""
Запуск веб-интерфейса:  python -m web_client

Переменные окружения:
    WEB_HOST     адрес (по умолчанию 0.0.0.0)
    WEB_PORT     порт (по умолчанию 5000)
    PARSER_URL   адрес сервиса разбора (по умолчанию http://localhost:8000)
"""

import os

from web_client.server import create_app


def main():
    host = os.environ.get('WEB_HOST', '0.0.0.0')
    port = int(os.environ.get('WEB_PORT', '5000'))
    parser_url = os.environ.get('PARSER_URL', 'http://localhost:8000')

    app = create_app(parser_url)
    print('=' * 50)
    print('  ДокАнализ — веб-интерфейс')
    print('  http://localhost:%d' % port)
    print('  Сервис разбора: %s' % parser_url)
    print('=' * 50)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
