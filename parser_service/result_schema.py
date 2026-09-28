"""
Сборка результата разбора в формат, который сервис отдаёт наружу.

Формат описан в docs/parser_api.md. Любое изменение здесь — изменение
договора с потребителями: несовместимое требует поднять SCHEMA_VERSION.
"""

SCHEMA_VERSION = '1.0'

STANDARD_FIELDS = (
    ('total', 'Сумма'),
    ('date', 'Дата'),
    ('company', 'Компания'),
    ('invoice_number', 'Номер счёта'),
    ('vat', 'НДС'),
    ('currency', 'Валюта'),
)

_EMPTY = ('', 'N/A', 'None', 'null', '-')


def _is_empty(value):
    return value is None or str(value).strip() in _EMPTY


def _plain(value):
    """Значение в JSON-совместимом виде: числа и строки как есть, прочее строкой."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _box(block, page):
    return {
        'page': page,
        'x': int(block.get('left', 0)),
        'y': int(block.get('top', 0)),
        'w': int(block.get('width', 0)),
        'h': int(block.get('height', 0)),
        'text': str(block.get('text', '')),
        'confidence': _plain(block.get('confidence')),
    }


def _union(boxes):
    if not boxes:
        return None
    left = min(b['x'] for b in boxes)
    top = min(b['y'] for b in boxes)
    right = max(b['x'] + b['w'] for b in boxes)
    bottom = max(b['y'] + b['h'] for b in boxes)
    return {'x': left, 'y': top, 'w': right - left, 'h': bottom - top}


def build_fields(record, mapping, page):
    """Поля одной записи со значениями, источником и координатами."""
    by_rules_record = record.get('extracted_by') == 'rules'
    rule_labels = set(record.get('_rule_fields') or [])
    fields = []

    for key, label in STANDARD_FIELDS:
        value = record.get(key)
        if _is_empty(value):
            continue
        boxes = [_box(b, page) for b in (mapping.get(key) or [])]
        fields.append({
            'name': key,
            'label': label,
            'kind': 'standard',
            'value': _plain(value),
            'source': 'rules' if by_rules_record else 'model',
            'boxes': boxes,
            'bbox': _union(boxes),
        })

    for label, value in (record.get('extra_fields') or {}).items():
        if _is_empty(value):
            continue
        boxes = [_box(b, page) for b in (mapping.get('extra::%s' % label) or [])]
        fields.append({
            'name': label,
            'label': label,
            'kind': 'extra',
            'value': _plain(value),
            'source': 'rules' if (by_rules_record or label in rule_labels) else 'model',
            'boxes': boxes,
            'bbox': _union(boxes),
        })
    return fields


def build_document(index, record, mapping, page):
    fields = build_fields(record, mapping, page)
    errors = record.get('validation_errors') or []
    return {
        'index': index,
        'page': page,
        'document_type': _plain(record.get('document_type')),
        'category': _plain(record.get('category')),
        'extracted_by': 'rules' if record.get('extracted_by') == 'rules' else 'model',
        'validation': {
            'valid': bool(record.get('validation_valid', not errors)),
            'errors': [str(e) for e in errors],
        },
        'fields': fields,
        'fields_total': len(fields),
        'fields_located': sum(1 for f in fields if f['boxes']),
    }


def build_page(ocr_page, image_url):
    return {
        'number': ocr_page['page'],
        'width': int(ocr_page.get('img_width') or 0),
        'height': int(ocr_page.get('img_height') or 0),
        'image_url': image_url,
        'table_grid': ocr_page.get('grid'),
        'words': [
            {
                'x': int(b.get('left', 0)), 'y': int(b.get('top', 0)),
                'w': int(b.get('width', 0)), 'h': int(b.get('height', 0)),
                'text': str(b.get('text', '')),
                'confidence': _plain(b.get('confidence')),
            }
            for b in ocr_page.get('blocks', [])
        ],
    }


def build_summary(documents):
    """Короткая выжимка для списка разборов (история в интерфейсе)."""
    first = documents[0] if documents else None
    values = {f['name']: f['value'] for f in (first['fields'] if first else [])
              if f['kind'] == 'standard'}
    return {
        'documents': len(documents),
        'fields': sum(d['fields_total'] for d in documents),
        'fields_located': sum(d['fields_located'] for d in documents),
        'document_type': first['document_type'] if first else None,
        'category': first['category'] if first else None,
        'total': values.get('total'),
        'currency': values.get('currency'),
    }


def build_result(parse_id, filename, records, mappings, ocr_pages, image_urls,
                 engine, logs=None, warnings=None):
    documents = []
    for i, (record, mapping) in enumerate(zip(records, mappings)):
        page = record.get('_page') or (i + 1)
        documents.append(build_document(i + 1, record, mapping, page))

    return {
        'schema_version': SCHEMA_VERSION,
        'id': parse_id,
        'status': 'done',
        'source': {'filename': filename},
        'engine': engine,
        'coordinate_system': {
            'unit': 'px',
            'origin': 'top-left',
            'relative_to': 'pages[].image_url — картинка страницы, по которой шло распознавание',
        },
        'pages': [build_page(p, image_urls.get(p['page'])) for p in ocr_pages],
        'documents': documents,
        'summary': build_summary(documents),
        'warnings': list(warnings or []),
        'logs': list(logs or []),
    }
