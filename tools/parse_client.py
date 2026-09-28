"""
Консольный потребитель сервиса разбора.

Показывает весь цикл без браузера: отправить файл → получить ID → по ID
забрать результат. Годится и как пример интеграции для другой системы,
и как быстрая проверка, что сервис жив и разбирает документы.

    python tools/parse_client.py submit баланс.pdf --wait
    python tools/parse_client.py status <ID>
    python tools/parse_client.py fields <ID>
    python tools/parse_client.py result <ID> -o результат.json

Адрес сервиса — --url или переменная PARSER_URL (по умолчанию
http://localhost:8000). Нужен только пакет requests.
"""

import argparse
import json
import os
import sys
import time

import requests


def api(base, method, path, **kwargs):
    resp = requests.request(method, base.rstrip('/') + '/api/v1' + path, timeout=(5, 120), **kwargs)
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code >= 400:
        message = (body or {}).get('error', {}).get('message') if isinstance(body, dict) else None
        sys.exit('Ошибка %d: %s' % (resp.status_code, message or resp.text[:200]))
    return body


def wait(base, parse_id, interval=2.0):
    shown = None
    while True:
        view = api(base, 'GET', '/parses/' + parse_id)
        state = view['status']
        if state != shown:
            extra = ' (№%s в очереди)' % view['queue_position'] if view.get('queue_position') else ''
            print('  статус: %s%s' % (state, extra), file=sys.stderr)
            shown = state
        if state in ('done', 'failed'):
            return view
        time.sleep(interval)


def print_fields(result):
    for doc in result['documents']:
        print('Стр. %s · %s · %s · поля: %d, с координатами: %d' % (
            doc['page'], doc.get('document_type') or '—', doc['extracted_by'],
            doc['fields_total'], doc['fields_located']))
        for f in doc['fields']:
            where = ('x=%(x)d y=%(y)d w=%(w)d h=%(h)d' % f['bbox']) if f['bbox'] else 'не найдено на странице'
            print('  %-28s %-24s [%s] %s' % (f['label'][:28], str(f['value'])[:24], f['source'], where))
    for w in result.get('warnings') or []:
        print('⚠ ' + w)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Клиент сервиса разбора ДокАнализ')
    parser.add_argument('--url', default=os.environ.get('PARSER_URL', 'http://localhost:8000'))
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('submit', help='отправить файл на разбор и напечатать ID')
    p.add_argument('file')
    p.add_argument('--wait', action='store_true', help='дождаться результата и показать поля')

    sub.add_parser('status', help='статус разбора').add_argument('id')
    sub.add_parser('fields', help='поля с координатами').add_argument('id')
    p = sub.add_parser('result', help='результат в JSON')
    p.add_argument('id')
    p.add_argument('-o', '--output', help='сохранить в файл вместо вывода на экран')

    args = parser.parse_args(argv)
    base = args.url

    if args.command == 'submit':
        with open(args.file, 'rb') as f:
            created = api(base, 'POST', '/parses',
                          files={'file': (os.path.basename(args.file), f)})
        print(created['id'])
        if args.wait:
            view = wait(base, created['id'])
            if view['status'] == 'failed':
                sys.exit('Разбор не удался: %s' % view.get('error'))
            print_fields(view['result'])
    elif args.command == 'status':
        view = api(base, 'GET', '/parses/' + args.id)
        view.pop('result', None)
        print(json.dumps(view, ensure_ascii=False, indent=2))
    elif args.command == 'fields':
        view = wait(base, args.id)
        if view['status'] == 'failed':
            sys.exit('Разбор не удался: %s' % view.get('error'))
        print_fields(view['result'])
    elif args.command == 'result':
        result = api(base, 'GET', '/parses/%s/result' % args.id)
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(text)
            print('Сохранено: ' + args.output, file=sys.stderr)
        else:
            print(text)


if __name__ == '__main__':
    main()
