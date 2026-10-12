"""
Сервер веб-интерфейса: отдаёт две страницы и пересылает /api/v1/* в сервис разбора.

Страницы разделены по назначению:
  /parse (и /)       — запуск разбора: загрузка файлов, ID разбора, статус,
                       история всех разборов;
  /view?id=<ID>      — просмотр одного конкретного разбора с подсветкой.
Связаны они только ID разбора: первая его выдаёт, вторая по нему открывает
результат.

Пересылка нужна, чтобы браузер ходил на один адрес: сервис разбора может
жить где угодно (другой контейнер, другая машина), а страница об этом не
знает — она всегда обращается к /api/v1 своего же сервера.
"""

import os

import requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

# Заголовки, которые относятся к конкретному соединению и пересылать их нельзя
_HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailers', 'transfer-encoding', 'upgrade', 'content-encoding',
               'content-length', 'host'}

# Разбор идёт в фоне, поэтому любой запрос к API отвечает быстро. Долгой
# бывает только загрузка большого файла.
TIMEOUT_SEC = (5, 120)


def create_app(parser_url, session=None):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
    app.json.ensure_ascii = False
    parser_url = parser_url.rstrip('/')
    http = session or requests.Session()

    @app.get('/')
    def index():
        # Ссылки старого вида /?id=<ID> (до разделения страниц) ведут в просмотр
        if request.args.get('id'):
            return redirect('/view?id=' + request.args['id'], code=302)
        return send_from_directory(STATIC_DIR, 'parse.html')

    @app.get('/parse')
    def parse_page():
        return send_from_directory(STATIC_DIR, 'parse.html')

    @app.get('/view')
    def view_page():
        return send_from_directory(STATIC_DIR, 'view.html')

    # Общие стили и скрипт обеих страниц
    @app.get('/static/<path:name>')
    def static_file(name):
        return send_from_directory(STATIC_DIR, name)

    @app.route('/api/v1/<path:path>', methods=['GET', 'POST', 'DELETE'])
    def proxy(path):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        try:
            upstream = http.request(
                request.method,
                '%s/api/v1/%s' % (parser_url, path),
                params=request.args,
                data=request.get_data(),
                headers=headers,
                timeout=TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            return jsonify({'error': {
                'code': 'parser_unavailable',
                'message': 'Сервис разбора недоступен (%s): %s' % (parser_url, exc.__class__.__name__),
            }}), 502
        out_headers = [(k, v) for k, v in upstream.headers.items()
                       if k.lower() not in _HOP_BY_HOP]
        return Response(upstream.content, status=upstream.status_code, headers=out_headers)

    return app
