#!/usr/bin/env python3
"""
Выгрузка разборов в папку-сайт, которая работает без сервиса (GitHub Pages).

Что получается в папке (по умолчанию ./dokanaliz-demo):

    index.html              простая страница: index.html?id=<ID> сразу открывает preview
    view.html               просмотр разбора; берёт <ID>.json из этой же папки
    static/common.js, .css  общее для просмотра
    <ID>.json               результат разбора — ровно как его отдаёт сервис
    raw/<ID>.json           сырой JSON (ответ OCR до обработки)
    pages/<ID>/page-N.jpg   картинки страниц, к ним относятся координаты
    files/<имя>.pdf         оригиналы документов (если указана папка с ними)
    board.tsv               строки для таблицы: year, quarter, file, json, preview

Запуск (сервис ДокАнализ должен быть запущен):

    python3 tools/export_static.py tools/smartpartner.csv --files ~/smartpartner

Список разборов — CSV с колонками year,quarter,document,id.
Картинки страниц сохраняются в JPEG: PNG со скана 300 dpi весит мегабайты, и
GitHub его не принимает (сайт — «file is too large», git по HTTPS — ошибка 408).
Размер в пикселях тот же, поэтому координаты из JSON остаются верными.
Для этого нужен Pillow: sudo apt install python3-pil. Без него — --images png.
"""
import argparse
import csv
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

JPEG_QUALITY = 80

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(ROOT, 'web_client', 'static')
EMBED_PAGE = os.path.join(ROOT, 'embed-example', 'index.html')


def fetch(service, path):
    url = service.rstrip('/') + path
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError('%s — HTTP %s' % (url, e.code)) from None
    except urllib.error.URLError as e:
        raise RuntimeError('%s — сервис не отвечает (%s). Он запущен?' % (url, e.reason)) from None


def pretty(data):
    """JSON с отступами: файлы прикрепляют в таблицу и читают глазами."""
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def save_page_image(png_bytes, path_without_ext, fmt):
    """Картинка страницы: PNG как есть или JPEG того же размера в пикселях."""
    if fmt == 'png':
        write(path_without_ext + '.png', png_bytes)
        return
    from PIL import Image
    import io
    with Image.open(io.BytesIO(png_bytes)) as img:
        size = img.size
        rgb = img.convert('L' if img.mode in ('1', 'L', 'LA') else 'RGB')
        os.makedirs(os.path.dirname(path_without_ext), exist_ok=True)
        rgb.save(path_without_ext + '.jpg', 'JPEG', quality=JPEG_QUALITY,
                 optimize=True, progressive=True)
    with Image.open(path_without_ext + '.jpg') as check:   # координаты — к этому размеру
        assert check.size == size, (check.size, size)


def switch_on_static(text, name):
    """Включает в странице режим «разбор из файлов рядом» (флаг STATIC_DATA)."""
    for old, new in (('const STATIC_DATA = false;', 'const STATIC_DATA = true;'),
                     ('var STATIC_DATA = false;', 'var STATIC_DATA = true;')):
        if old in text:
            return text.replace(old, new, 1)
    sys.exit('В %s нет флага STATIC_DATA — обновите веб-интерфейс (web-update-14).' % name)


def copy_pages(out, image_fmt='jpg'):
    """Страницы сайта: просмотр и простая страница, обе в режиме «из файлов»."""
    with open(os.path.join(STATIC_DIR, 'view.html'), encoding='utf-8') as f:
        view = switch_on_static(f.read(), 'view.html')
    ext = "const STATIC_IMAGE_EXT = 'png';"
    if ext not in view:
        sys.exit('В view.html нет STATIC_IMAGE_EXT — обновите веб-интерфейс (web-update-15).')
    view = view.replace(ext, "const STATIC_IMAGE_EXT = '%s';" % image_fmt, 1)
    # На сервисе общие файлы лежат в /static/, на GitHub — в папке рядом
    view = view.replace('href="/static/', 'href="static/').replace('src="/static/', 'src="static/')
    with open(os.path.join(out, 'view.html'), 'w', encoding='utf-8') as f:
        f.write(view)

    with open(EMBED_PAGE, encoding='utf-8') as f:
        page = switch_on_static(f.read(), 'embed-example/index.html')
    with open(os.path.join(out, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(page)

    os.makedirs(os.path.join(out, 'static'), exist_ok=True)
    for name in ('common.js', 'common.css'):
        shutil.copyfile(os.path.join(STATIC_DIR, name), os.path.join(out, 'static', name))
    # GitHub Pages без Jekyll: файлы отдаются как есть
    open(os.path.join(out, '.nojekyll'), 'w').close()


def find_original(files_dir, filename):
    if not files_dir or not filename:
        return None
    path = os.path.join(files_dir, filename)
    if os.path.isfile(path):
        return path
    for name in os.listdir(files_dir):                # регистр в имени мог отличаться
        if name.lower() == filename.lower():
            return os.path.join(files_dir, name)
    return None


def export_one(service, out, files_dir, parse_id, image_fmt='jpg'):
    """Один разбор -> файлы в папке. Отдаёт имя файла оригинала (или None)."""
    result = json.loads(fetch(service, '/api/v1/parses/%s/result' % parse_id))
    write(os.path.join(out, parse_id + '.json'), pretty(result))  # содержимое как есть

    try:
        write(os.path.join(out, 'raw', parse_id + '.json'),
              pretty(json.loads(fetch(service, '/api/v1/parses/%s/raw' % parse_id))))
    except RuntimeError as e:
        print('   ! сырого JSON нет (%s)' % e)

    for page in result.get('pages', []):
        n = page['number']
        save_page_image(fetch(service, '/api/v1/parses/%s/pages/%d/image' % (parse_id, n)),
                        os.path.join(out, 'pages', parse_id, 'page-%d' % n), image_fmt)

    filename = (result.get('source') or {}).get('filename')
    original = find_original(files_dir, filename)
    if original:
        target = os.path.join(out, 'files', os.path.basename(original))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(original, target)
        return os.path.basename(original), len(result.get('pages', []))
    if files_dir:
        print('   ! оригинал %s не найден в %s' % (filename, files_dir))
    return None, len(result.get('pages', []))


def main():
    ap = argparse.ArgumentParser(description='Выгрузка разборов в папку для GitHub Pages')
    ap.add_argument('list', help='CSV: year,quarter,document,id')
    ap.add_argument('--service', default='http://localhost:5000',
                    help='адрес ДокАнализа (по умолчанию http://localhost:5000)')
    ap.add_argument('--files', help='папка с оригиналами PDF')
    ap.add_argument('--out', default='dokanaliz-demo', help='куда сложить сайт')
    ap.add_argument('--site', default='https://reyni300.github.io/dokanaliz-demo/',
                    help='адрес сайта на GitHub Pages — для ссылок в board.tsv')
    ap.add_argument('--images', choices=('jpg', 'png'), default='jpg',
                    help='формат картинок страниц (по умолчанию jpg — лёгкий для GitHub)')
    args = ap.parse_args()

    files_dir = os.path.expanduser(args.files) if args.files else None
    if files_dir and not os.path.isdir(files_dir):
        sys.exit('Папки с оригиналами нет: %s' % files_dir)
    out = os.path.abspath(args.out)
    site = args.site.rstrip('/') + '/'
    os.makedirs(out, exist_ok=True)

    with open(args.list, encoding='utf-8') as f:
        rows = [r for r in csv.DictReader(f) if (r.get('id') or '').strip()]

    if args.images == 'jpg':
        try:
            import PIL  # noqa: F401
        except ImportError:
            sys.exit('Для JPEG нужен Pillow: sudo apt install python3-pil '
                     '(или запустите с --images png)')
    copy_pages(out, args.images)
    board = [('year', 'quarter', 'document', 'file', 'json', 'preview')]
    failed = 0
    for r in rows:
        pid = r['id'].strip().lower()
        print('%s %s %s — %s' % (r['year'], r['quarter'], r['document'], pid))
        try:
            original, pages = export_one(args.service, out, files_dir, pid, args.images)
        except RuntimeError as e:
            print('   ✗ %s' % e)
            failed += 1
            continue
        print('   ✓ страниц: %d%s' % (pages, ', оригинал: ' + original if original else ''))
        board.append((
            r['year'], r['quarter'], r['document'],
            site + 'files/' + urllib.parse.quote(original) if original else '',
            site + pid + '.json',
            site + '?id=' + pid,
        ))

    with open(os.path.join(out, 'board.tsv'), 'w', encoding='utf-8') as f:
        f.write('\n'.join('\t'.join(row) for row in board) + '\n')

    print('\nГотово: %s' % out)
    print('Строки для таблицы: %s' % os.path.join(out, 'board.tsv'))
    if failed:
        print('Не выгружено разборов: %d — см. ✗ выше' % failed)
        sys.exit(1)


if __name__ == '__main__':
    main()
