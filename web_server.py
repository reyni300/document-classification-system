"""
web_server.py — Flask-сервер для веб-интерфейса ДокАнализ
Запуск: python web_server.py
Открыть:  http://localhost:5000
"""

import os
import sys
import json
import re
import tempfile
from flask import Flask, request, jsonify, send_from_directory, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrator.orchestrator import Orchestrator
from ocr.ocr_module import OCRModule
from llm.llm_agent import LLMAgent
from db.db_module import DatabaseModule
from agents.classification_agent import ClassificationAgent
from agents.validation_agent import ValidationAgent
from llm.llm_client import LLMClient
from translation.translator import translate_field
import rule_fields
import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

ocr       = OCRModule()
llm_agent = LLMAgent()
db        = DatabaseModule(config['database']['file'])
classifier = ClassificationAgent(LLMClient())
validator  = ValidationAgent()

orchestrator = Orchestrator(
    ocr_module=ocr,
    llm_agent=llm_agent,
    db_module=db,
    classifier=classifier,
    validator=validator,
    translator_func=translate_field if config['app'].get('show_translation', True) else None
)

PDF_PAGES_DIR = os.path.join(tempfile.gettempdir(), "dokanaliz_pages")
os.makedirs(PDF_PAGES_DIR, exist_ok=True)

app = Flask(__name__, static_folder='.', static_url_path='')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


_NUMBER_TOKEN_RE = re.compile(r'^(\d+)[.,](\d+)$')


def _canon_number_token(token):
    """
    "1.20" -> "1.2", "17.00" -> "17", "18.20" -> "18.2".

    Валидаторы в llm_agent.py приводят денежные значения к двум знакам после
    запятой (normalize_total / normalize_vat), а в документе напечатано как
    есть: "17", "1.2". Дописанные нули ломали сопоставление: нормализация
    выбрасывает точку, и "1.20 BYN" превращалось в "120byn", что уже НЕ
    является подстрокой напечатанного "12byn" — значение не находилось нигде.
    Именно так пропадали "Сумма" и "Комиссия" на чеке Альфа-Банка.

    Обрезаем только хвостовые нули дробной части и только у токена, который
    целиком является числом: дату "01.03.2026" трогать нельзя (там два
    разделителя, полное совпадение не сработает, и токен остаётся как был).
    """
    m = _NUMBER_TOKEN_RE.match(token)
    if not m:
        return token
    frac = m.group(2).rstrip('0')
    return m.group(1) + ('.' + frac if frac else '')


def normalize_for_search(text):
    """Нормализует строку для нечёткого сравнения."""
    if not text:
        return ''
    text = str(text).lower().strip()
    text = ' '.join(_canon_number_token(tok) for tok in text.split())
    text = re.sub(r'[^\w\d]', '', text)
    return text


def blocks_in_reading_order(blocks):
    """
    Раскладывает OCR-блоки в порядке чтения: сначала собирает их в строки по
    вертикальному перекрытию, затем внутри каждой строки сортирует слева
    направо. Нужно для поиска ФРАЗЫ по подряд идущим блокам: Tesseract отдаёт
    отдельный блок на каждое слово, и "Дебетовая карта (**** 0000)" — это
    четыре соседних блока одной строки, а не один.
    """
    items = [b for b in blocks if str(b.get('text', '')).strip()]
    if not items:
        return []

    lines = []
    for b in sorted(items, key=lambda x: (x.get('top', 0), x.get('left', 0))):
        top = b.get('top', 0)
        bottom = top + b.get('height', 0)
        placed = False
        for line in lines:
            l_top = min(x.get('top', 0) for x in line)
            l_bottom = max(x.get('top', 0) + x.get('height', 0) for x in line)
            overlap = min(bottom, l_bottom) - max(top, l_top)
            shortest = min(bottom - top, l_bottom - l_top) or 1
            if overlap > 0.4 * shortest:
                line.append(b)
                placed = True
                break
        if not placed:
            lines.append([b])

    lines.sort(key=lambda line: min(x.get('top', 0) for x in line))
    ordered = []
    for line in lines:
        ordered.extend(sorted(line, key=lambda b: b.get('left', 0)))
    return ordered


def _window_score(window_text, norm_value, fragment_counts=None):
    """
    Насколько склейка подряд идущих блоков соответствует значению.
    1.0 — точное совпадение; меньше — если в окне есть лишнее или, наоборот,
    покрыта только часть значения. 0 — не годится.
    """
    if not window_text:
        return 0.0
    if window_text == norm_value:
        return 1.0
    if norm_value in window_text:
        # Если значение — число, оно должно совпасть с ЦЕЛЫМ числом в окне, а
        # не с куском более длинного: "6726" внутри "672600" — это другое
        # число. На чеке Альфа-Банка из-за этого значение "6726.00" садилось на
        # блок кода авторизации "672600". Проверяем именно по числам в окне, а
        # не по строке целиком: иначе ограничение обходится соседним текстовым
        # блоком ("6174860" совпадало с окном "RRN" + "617486005488").
        if norm_value.isdigit():
            if norm_value not in re.findall(r'\d+', window_text):
                return 0.0
        # Значение целиком внутри окна, но есть лишние символы — чем их
        # меньше, тем лучше.
        return len(norm_value) / len(window_text)
    if window_text in norm_value:
        # Окно покрывает только ЧАСТЬ значения. Так бывает законно: дата
        # "01.03.2026" в бланке разбита на "01", "марта", "20", "26", и целиком
        # её последовательностью блоков не собрать.
        value_has_digits = any(ch.isdigit() for ch in norm_value)
        window_has_digits = any(ch.isdigit() for ch in window_text)

        # У денежного значения опознавательный кусок — это ЧИСЛО, а не код
        # валюты. На чеке Альфа-Банка "1.2 BYN" из строки "Комиссия" цеплялось
        # за единственный распознанный "BYN" (он оказался на строке "Сумма") и
        # вставало на чужую строку, подменяя собой сумму. Кусок без цифр не
        # может опознать место, если в самом значении цифры есть.
        if value_has_digits and not window_has_digits:
            return 0.0

        # Числовой кусок различает место и при двух символах: "1.2" из строки
        # "Комиссия" нормализуется в "12", и раньше он отбрасывался порогом в
        # три символа — притом что это был единственный ПРАВИЛЬНЫЙ якорь.
        if len(window_text) < (2 if window_has_digits else 3):
            return 0.0

        # Число значения совпало ЦЕЛИКОМ, не хватает только обрамления — как
        # правило кода валюты, который OCR распознаёт через раз. Это сильное
        # совпадение: в "1.2 BYN" опознаёт место именно "1.2". По длине строки
        # такой кусок набирал бы всего 2 символа из 5 и не дотягивал до порога.
        if value_has_digits:
            value_digits = ''.join(ch for ch in norm_value if ch.isdigit())
            window_digits = ''.join(ch for ch in window_text if ch.isdigit())
            if window_digits and window_digits == value_digits:
                return 0.85

        coverage = len(window_text) / len(norm_value)

        # Кусок должен ещё и однозначно указывать на место в документе:
        # фрагмент, встречающийся в документе много раз, сам по себе не
        # доказывает ничего.
        occurrences = (fragment_counts or {}).get(window_text, 1)
        if occurrences > 1 and coverage < 0.75:
            return 0.0

        if coverage >= 0.35:
            return 0.9 * coverage
    return 0.0


def find_blocks_for_value(blocks, value):
    """
    Ищет OCR-блоки, соответствующие извлечённому значению.

    Раньше блоки искались ПОШТУЧНО и независимо друг от друга, причём ветки
    шли по очереди: если срабатывал поиск "по первому числу значения", то
    словесная часть значения уже не проверялась вовсе. На реальном чеке это
    давало съехавшие рамки: у значения "Дебетовая карта (**** 0000)" бралось
    первое число "0000", и подсвечивались все блоки, где оно встречается, —
    хвост "0000)" вместо всей фразы плюс лишняя рамка на совсем другой строке
    с номером карточки "000000******0000".

    Теперь ищем ПОСЛЕДОВАТЕЛЬНОСТЬ подряд идущих блоков (в порядке чтения),
    склейка которых лучше всего совпадает со значением целиком. Значение,
    встречающееся в документе несколько раз, даёт несколько одинаково хороших
    последовательностей — возвращаем их все.
    """
    if not value or not blocks:
        return []

    value_str = str(value).strip()
    norm_value = normalize_for_search(value_str)

    if not norm_value or len(norm_value) < 2:
        return []

    # Короткие значения (НДС "20", код валюты "BYN") — только ТОЧНОЕ совпадение,
    # без частичных. Обнаружено на реальном OCR: "20" это подстрока "05092026"
    # (год в дате), и без строгой проверки vat и date садились на один блок.
    #
    # Но совпасть может и НЕСКОЛЬКО подряд идущих блоков: на чеках Tesseract
    # регулярно разбивает код валюты на "BY" + "N", и такая строка оставалась
    # без подсветки. Требование точного равенства при этом сохраняется — окно
    # принимается, только если склейка равна значению целиком.
    if len(norm_value) <= 3:
        ordered = blocks_in_reading_order(blocks)
        norms = [normalize_for_search(b.get('text', '')) for b in ordered]
        matched, used = [], set()
        for i in range(len(ordered)):
            if i in used or not norms[i]:
                continue
            acc = ''
            for j in range(i, min(len(ordered), i + 4)):
                acc += norms[j]
                if len(acc) > len(norm_value):
                    break
                if acc == norm_value:
                    matched.extend(ordered[i:j + 1])
                    used.update(range(i, j + 1))
                    break
        return matched

    ordered = blocks_in_reading_order(blocks)
    if not ordered:
        return []

    norms = [normalize_for_search(b.get('text', '')) for b in ordered]
    n = len(ordered)
    MAX_BLOCKS_IN_WINDOW = 14

    # Первый проход: перебираем окна и считаем, в скольких РАЗНЫХ местах
    # документа встречается одна и та же склейка текста. Это нужно, чтобы
    # отличить надёжный кусок значения от расхожего фрагмента вроде "BYN"
    # (см. _window_score).
    windows = []  # (start, end, acc)
    fragment_counts = {}
    for i in range(n):
        if not norms[i]:
            continue
        acc = ''
        for j in range(i, min(n, i + MAX_BLOCKS_IN_WINDOW)):
            # Блоки из одной пунктуации ("(****", ":", "—") нормализуются в
            # пустую строку. Такой блок не разрывает фразу — он её часть:
            # в "Дебетовая карта (**** 0000)" между "карта" и "0000)" стоит
            # именно он, и если на нём останавливаться, рамка обрывается на
            # середине фразы.
            acc += norms[j]
            if len(acc) > len(norm_value) + 12:
                break  # окно уже заведомо длиннее значения — дальше только хуже
            if acc:
                windows.append((i, j, acc))
                fragment_counts[acc] = fragment_counts.get(acc, 0) + 1

    # Второй проход: оцениваем окна, уже зная, какие фрагменты неоднозначны.
    candidates = []  # (score, start, end)
    for i, j, acc in windows:
        score = _window_score(acc, norm_value, fragment_counts)
        if score > 0:
            candidates.append((score, i, j))

    if not candidates:
        return []

    # Лучший счёт задаёт планку; при равном счёте предпочитаем более короткое
    # окно (меньше лишних блоков в рамке).
    candidates.sort(key=lambda c: (-c[0], c[2] - c[1]))
    top_score = candidates[0][0]
    if top_score < 0.4:
        return []

    matched = []
    used = set()
    for score, i, j in candidates:
        if score < top_score - 1e-9:
            break  # берём только все РАВНО ЛУЧШИЕ вхождения значения
        if any(k in used for k in range(i, j + 1)):
            continue  # пересекается с уже выбранным вхождением
        matched.extend(ordered[i:j + 1])
        used.update(range(i, j + 1))

    return matched


def build_field_mapping(parsed_item, blocks):
    """
    Для каждого поля из parsed_item находит соответствующие OCR-блоки.
    Возвращает dict: { field_name: [{ text, left, top, width, height, confidence }] }
    """
    fields_to_map = ['total', 'date', 'company', 'invoice_number', 'vat', 'currency']
    mapping = {}

    # Основные поля
    for field in fields_to_map:
        value = parsed_item.get(field)
        if value and str(value).strip() and str(value).strip() not in ('N/A', 'None', ''):
            found_blocks = find_blocks_for_value(blocks, value)
            if found_blocks:
                mapping[field] = found_blocks

    # extra_fields — ключ формата "extra::<название поля>"
    extra = parsed_item.get('extra_fields') or {}
    for label, value in extra.items():
        if value and str(value).strip() not in ('N/A', 'None', '', '-'):
            found_blocks = find_blocks_for_value(blocks, value)
            if found_blocks:
                mapping[f'extra::{label}'] = found_blocks

    return mapping


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/api/page-image/<path:filename>')
def page_image(filename):
    """Отдаёт PNG-версию страницы PDF, конвертированную OCR-модулем."""
    path = os.path.join(PDF_PAGES_DIR, filename)
    if not os.path.exists(path):
        return jsonify({'error': 'Страница не найдена'}), 404
    return send_file(path, mimetype='image/png')


@app.route('/api/process', methods=['POST'])
def process_document():
    if 'file' not in request.files:
        return jsonify({'error': 'Файл не передан'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Имя файла пустое'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': f'Формат не поддерживается: {file.filename}'}), 400

    suffix = '.' + file.filename.rsplit('.', 1)[1].lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name
    print(f"DEBUG tmp_path: {tmp_path}, exists: {os.path.exists(tmp_path)}")

    try:
        # Сбрасываем кэш LLM перед каждым новым файлом
        llm_agent._last_ocr_hash = None
        llm_agent._last_result = None

        # Запускаем основную обработку.
        # ВАЖНО: раньше здесь ДО оркестратора отдельно вызывался ocr.extract(tmp_path)
        # только чтобы получить блоки под маппинг полей на фронте. Для картинок это
        # было небезопасно: оркестратор внутри себя гоняет OCR по ДРУГОМУ варианту
        # изображения (после preprocess_image — контраст/резкость/жёсткая бинаризация),
        # а этот отдельный вызов — по оригиналу. Два прохода Tesseract по разным
        # картинкам вполне могут распознать текст по-разному, и тогда
        # build_field_mapping() не находил бы блок под значение, которое реально
        # извлёк LLM (LLM ведь читал текст ИЗ ДРУГОГО прохода OCR). Плюс PDF гонялся
        # через pdf2image+Tesseract дважды впустую.
        # Теперь оркестратор сам отдаёт наверх ocr_pages — те же страницы/блоки,
        # что видел LLM, — и мы строим маппинг по ним же. Один OCR-проход на файл.
        result = orchestrator.process_document(tmp_path, file.filename)
        ocr_pages = result.get('ocr_pages', [])
        pages_blocks = [page.get('blocks', []) for page in ocr_pages]
        pages_grid = [page.get('grid') for page in ocr_pages]

        # Строим маппинг для каждой записи в parsed_list
        parsed_list = result.get('parsed_list', [])
        field_mappings = []

        for idx, parsed_item in enumerate(parsed_list):
            # Определяем страницу для этой записи
            page_idx = parsed_item.get('_page', idx + 1) - 1
            blocks = pages_blocks[page_idx] if page_idx < len(pages_blocks) else \
                     (pages_blocks[0] if pages_blocks else [])
            grid = pages_grid[page_idx] if page_idx < len(pages_grid) else \
                   (pages_grid[0] if pages_grid else None)

            mapping = build_field_mapping(parsed_item, blocks)
            added = rule_fields.merge_fields(parsed_item, blocks, mapping, grid=grid)
            if added:
                print(f'[rule_fields] стр. {page_idx + 1}: добавлено полей — {added}')
            field_mappings.append(mapping)

        # Добавляем блоки страниц, маппинг и PNG-урлы в результат
        result['pages_blocks'] = pages_blocks

        # PNG-урлы для предпросмотра PDF-страниц
        png_urls = []
        for page in ocr_pages:
            png_path = page.get('png_path', '')
            if png_path and png_path.startswith(PDF_PAGES_DIR):
                fname = os.path.basename(png_path)
                png_urls.append(f'/api/page-image/{fname}')
            else:
                png_urls.append(None)
        result['png_urls'] = png_urls
        result['field_mappings'] = field_mappings
        result.pop('ocr_pages', None)  # сырые блоки с полным текстом каждого слова фронту не нужны

        return jsonify(result)

    except Exception as e:
        return jsonify({
            'error': str(e),
            'filename': file.filename,
            'parsed_list': [],
            'logs': [],
            'pages_blocks': [],
            'field_mappings': []
        }), 500
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass


@app.route('/api/history', methods=['GET'])
def get_history():
    docs = db.get_all()
    result = []
    for doc_id, filename, created_at in docs:
        row = db.get_one(doc_id)
        total, currency, category = '—', '', 'прочее'
        if row:
            _, parsed = row
            first = parsed[0] if isinstance(parsed, list) and parsed else parsed
            if isinstance(first, dict):
                total    = first.get('total', '—')
                currency = first.get('currency', '')
                category = first.get('category', 'прочее')
        result.append({
            'id':       doc_id,
            'filename': filename,
            'date':     (created_at or '')[:10],
            'total':    total,
            'currency': currency,
            'category': category,
        })
    return jsonify(result)


@app.route('/api/document/<int:doc_id>', methods=['GET'])
def get_document(doc_id):
    data = db.get_one(doc_id)
    if not data:
        return jsonify({'error': 'Документ не найден'}), 404
    ocr_text, parsed = data
    docs = db.get_all()
    filename = next((f for did, f, _ in docs if did == doc_id), '')
    return jsonify({'doc_id': doc_id, 'filename': filename, 'ocr_text': ocr_text, 'parsed': parsed})


@app.route('/api/document/<int:doc_id>', methods=['DELETE'])
def delete_document(doc_id):
    db.delete(doc_id)
    return jsonify({'ok': True})


if __name__ == '__main__':
    print("=" * 50)
    print("  ДокАнализ — Веб-сервер")
    print("  http://localhost:5000")
    print("=" * 50)
    app.run(debug=True, host='0.0.0.0', port=5000)
