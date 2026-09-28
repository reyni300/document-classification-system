"""
HTTP API сервиса разбора. Договор описан в docs/parser_api.md.

    POST   /api/v1/parses                        принять файл, вернуть ID
    GET    /api/v1/parses                        список разборов
    GET    /api/v1/parses/<id>                   статус; когда готово — и результат
    GET    /api/v1/parses/<id>/result            только результат (JSON-файл)
    GET    /api/v1/parses/<id>/pages/<n>/image   картинка страницы
    DELETE /api/v1/parses/<id>                   удалить разбор
    GET    /api/v1/health                        жив ли сервис
"""

import os

import requests
from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge

from . import __version__, storage
from .storage import ParseNotFound


API = '/api/v1'


def error(http_status, code, message, **extra):
    body = {'error': {'code': code, 'message': message}}
    body['error'].update(extra)
    return jsonify(body), http_status


def create_app(store, worker, max_upload_mb=25):
    app = Flask(__name__)
    app.config['MAX_CONTENT_LENGTH'] = max_upload_mb * 1024 * 1024
    app.json.ensure_ascii = False        # кириллица в ответах — как есть, не К
    app.json.sort_keys = False           # порядок полей — как в документе

    def links(parse_id):
        base = '%s/parses/%s' % (API, parse_id)
        return {'self': base, 'result': base + '/result'}

    def status_view(status, with_result=True):
        view = {
            'id': status['id'],
            'status': status['status'],
            'filename': status.get('filename'),
            'size_bytes': status.get('size_bytes'),
            'created_at': status.get('created_at'),
            'started_at': status.get('started_at'),
            'finished_at': status.get('finished_at'),
            'queue_position': worker.queue_position(status['id']),
            'error': status.get('error'),
            'summary': status.get('summary'),
            'links': links(status['id']),
        }
        if status['status'] == storage.FAILED and status.get('logs'):
            view['logs'] = status['logs']
        if with_result and status['status'] == storage.DONE:
            view['result'] = store.get_result(status['id'])
        return view

    # --- ошибки единым форматом ---------------------------------------------
    @app.errorhandler(ParseNotFound)
    def _not_found(_exc):
        return error(404, 'not_found', 'Разбор с таким ID не найден')

    @app.errorhandler(RequestEntityTooLarge)
    def _too_large(_exc):
        return error(413, 'file_too_large', 'Файл больше %d МБ' % max_upload_mb)

    @app.errorhandler(HTTPException)
    def _http(exc):
        return error(exc.code, exc.name.lower().replace(' ', '_'), exc.description)

    @app.errorhandler(Exception)
    def _unexpected(exc):
        app.logger.exception('Необработанная ошибка')
        return error(500, 'internal_error', str(exc) or exc.__class__.__name__)

    # CORS: результат по ID может забрать и страница с другого адреса.
    @app.after_request
    def _cors(resp):
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, DELETE, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp

    # --- маршруты ------------------------------------------------------------
    @app.post(API + '/parses')
    def create_parse():
        file = request.files.get('file')
        if file is None:
            return error(400, 'file_missing', 'Файл не передан: ожидается поле формы "file"')
        if not file.filename:
            return error(400, 'file_missing', 'У файла нет имени')
        if storage.extension_of(file.filename) not in storage.ALLOWED_EXTENSIONS:
            return error(415, 'unsupported_format',
                         'Формат не поддерживается: %s' % file.filename,
                         allowed=list(storage.ALLOWED_EXTENSIONS))

        parse_id = store.create(file.filename, file.save)
        worker.submit(parse_id)
        resp = jsonify(status_view(store.get_status(parse_id), with_result=False))
        resp.status_code = 202
        resp.headers['Location'] = links(parse_id)['self']
        return resp

    @app.get(API + '/parses')
    def list_parses():
        limit = request.args.get('limit', default=100, type=int)
        items = [status_view(s, with_result=False) for s in store.list(limit=max(1, limit))]
        return jsonify({'items': items, 'count': len(items)})

    @app.get(API + '/parses/<parse_id>')
    def get_parse(parse_id):
        return jsonify(status_view(store.get_status(parse_id)))

    @app.get(API + '/parses/<parse_id>/result')
    def get_result(parse_id):
        status = store.get_status(parse_id)
        if status['status'] != storage.DONE:
            return error(409, 'not_ready', 'Результата пока нет: разбор в состоянии "%s"'
                         % status['status'], status=status['status'])
        resp = send_file(store.result_path(parse_id), mimetype='application/json')
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @app.get(API + '/parses/<parse_id>/pages/<int:page>/image')
    def get_page_image(parse_id, page):
        store.get_status(parse_id)                      # 404, если разбора нет
        path = store.page_image_path(parse_id, page)
        if not path:
            return error(404, 'page_not_found', 'Страницы %d нет или она ещё не готова' % page)
        return send_file(path, mimetype='image/png', max_age=3600)

    @app.delete(API + '/parses/<parse_id>')
    def delete_parse(parse_id):
        status = store.get_status(parse_id)
        if status['status'] == storage.PROCESSING:
            return error(409, 'busy', 'Разбор сейчас обрабатывается, удалить можно после')
        store.delete(parse_id)
        return '', 204

    @app.get(API + '/health')
    def health():
        return jsonify({
            'status': 'ok',
            'service': 'parser_service',
            'version': __version__,
            'queue_length': worker.queue_length(),
            'processing': worker.current,
            'llm': llm_health(),
        })

    return app


def llm_health():
    """
    Отвечает ли Ollama. Сервис работает и без неё (поля соберут правила),
    но об этом лучше знать до того, как отправлять документы.
    """
    host = os.environ.get('OLLAMA_HOST', 'http://localhost:11434').rstrip('/')
    if '://' not in host:
        host = 'http://' + host
    try:
        resp = requests.get(host + '/api/tags', timeout=1.5)
        models = [m.get('name') for m in resp.json().get('models', [])]
        return {'url': host, 'reachable': resp.ok, 'models': models}
    except (requests.RequestException, ValueError, AttributeError):
        return {'url': host, 'reachable': False, 'models': []}
