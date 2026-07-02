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


def normalize_for_search(text):
    """Нормализует строку для нечёткого сравнения."""
    if not text:
        return ''
    text = str(text).lower().strip()
    text = re.sub(r'[^\w\d]', '', text)
    return text


def find_blocks_for_value(blocks, value):
    """
    Ищет OCR-блоки, соответствующие извлечённому значению.
    Возвращает список блоков с координатами.
    """
    if not value or not blocks:
        return []

    value_str = str(value).strip()
    norm_value = normalize_for_search(value_str)

    if not norm_value or len(norm_value) < 2:
        return []

    matched = []

    # 1. Точное совпадение нормализованного текста блока
    for block in blocks:
        norm_block = normalize_for_search(block.get('text', ''))
        if norm_block and norm_value in norm_block:
            matched.append(block)

    if matched:
        return matched

    # 2. Поиск по числам — для сумм, дат, номеров
    value_parts = re.findall(r'\d+', norm_value)
    if value_parts:
        main_number = value_parts[0]
        if len(main_number) >= 3:
            for block in blocks:
                norm_block = normalize_for_search(block.get('text', ''))
                if main_number in norm_block:
                    matched.append(block)

    if matched:
        return matched

    # 3. Поиск по значимым словам (для компаний типа "ЗАО Альфа-Банк")
    # Разбиваем значение на слова длиной 3+ символа и ищем блоки содержащие хотя бы одно
    words = [w for w in re.findall(r'[а-яёa-z0-9]+', norm_value) if len(w) >= 3]
    if words:
        for block in blocks:
            norm_block = normalize_for_search(block.get('text', ''))
            if norm_block and any(w in norm_block for w in words):
                matched.append(block)

    if matched:
        return matched

    # 4. Поиск по префиксу
    if len(norm_value) >= 4:
        prefix = norm_value[:4]
        for block in blocks:
            norm_block = normalize_for_search(block.get('text', ''))
            if norm_block and prefix in norm_block:
                matched.append(block)

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

        # Получаем OCR-блоки ДО оркестратора, пока файл точно на диске
        try:
            ocr_data_for_mapping = ocr.extract(tmp_path)
            pages_blocks = [page.get('blocks', []) for page in ocr_data_for_mapping.get('pages', [])]
        except Exception as e:
            print(f"DEBUG OCR mapping error: {e}")
            import traceback
            traceback.print_exc()
            pages_blocks = []

        # Сбрасываем кэш снова — чтобы оркестратор запустил LLM заново,
        # а не вернул кэш от вызова выше
        llm_agent._last_ocr_hash = None
        llm_agent._last_result = None

        # Запускаем основную обработку
        result = orchestrator.process_document(tmp_path, file.filename)

        # Строим маппинг для каждой записи в parsed_list
        parsed_list = result.get('parsed_list', [])
        field_mappings = []

        for idx, parsed_item in enumerate(parsed_list):
            # Определяем страницу для этой записи
            page_idx = parsed_item.get('_page', idx + 1) - 1
            blocks = pages_blocks[page_idx] if page_idx < len(pages_blocks) else \
                     (pages_blocks[0] if pages_blocks else [])

            mapping = build_field_mapping(parsed_item, blocks)
            field_mappings.append(mapping)

        # Добавляем блоки страниц, маппинг и PNG-урлы в результат
        result['pages_blocks'] = pages_blocks

        # PNG-урлы для предпросмотра PDF-страниц
        png_urls = []
        print(f"DEBUG PDF_PAGES_DIR={PDF_PAGES_DIR}")
        for page in ocr_data_for_mapping.get('pages', []):
            png_path = page.get('png_path', '')
            print(f"DEBUG png_path={png_path}, starts={png_path.startswith(PDF_PAGES_DIR) if png_path else False}")
            if png_path and png_path.startswith(PDF_PAGES_DIR):
                fname = os.path.basename(png_path)
                png_urls.append(f'/api/page-image/{fname}')
            else:
                png_urls.append(None)
        result['png_urls'] = png_urls
        result['field_mappings'] = field_mappings
        
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
