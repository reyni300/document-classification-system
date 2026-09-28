"""
Разбор одного документа: OCR → модель → правила → координаты полей.

Здесь собрано то, что раньше было размазано между оркестратором и
веб-сервером: оркестратор распознавал документ, а поиск координат полей и
дочитывание правилами делал уже web_server.py перед отдачей в браузер.
Теперь весь разбор — внутри сервиса, наружу уходит готовый результат.

Тяжёлые модули (OpenCV, Tesseract, клиент модели) импортируются при
создании DocumentPipeline, а не при импорте файла: API и тесты API не
должны зависеть от них.
"""

import os

from PIL import Image

from . import __version__, result_schema
from .field_locator import build_field_mapping
from .worker import ParseError


PAGE_IMAGE_URL = '/api/v1/parses/{id}/pages/{n}/image'


class DocumentPipeline:
    def __init__(self):
        import pytesseract
        import yaml

        import rule_fields
        from agents.classification_agent import ClassificationAgent
        from agents.validation_agent import ValidationAgent
        from llm.llm_agent import LLMAgent
        from llm.llm_client import LLMClient
        from ocr.ocr_module import OCRModule
        from orchestrator.orchestrator import Orchestrator
        from translation.translator import translate_field

        with open('config.yaml', 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        self.rule_fields = rule_fields
        self.llm = LLMAgent()
        self.orchestrator = Orchestrator(
            ocr_module=OCRModule(),
            llm_agent=self.llm,
            db_module=None,                 # результат хранит сервис, не SQLite
            classifier=ClassificationAgent(LLMClient()),
            validator=ValidationAgent(),
            translator_func=(translate_field
                             if config['app'].get('show_translation', True) else None),
        )
        self.engine = {
            'ocr': 'Tesseract %s' % pytesseract.get_tesseract_version(),
            'llm': config['llm'].get('version', config['llm']['model']),
            'rules': getattr(rule_fields, '__version__', None),
            'parser_service': __version__,
        }

    def run(self, parse_id, source_path, filename, work_dir):
        # Кэш модели держит последний ответ по хэшу OCR-текста. Между разными
        # документами он не нужен, а вот вернуть чужой результат может.
        self.llm._last_ocr_hash = None
        self.llm._last_result = None

        out = self.orchestrator.process_document(source_path, filename, work_dir=work_dir)
        logs = out.get('logs') or []
        records = out.get('parsed_list') or []
        ocr_pages = out.get('ocr_pages') or []
        if not records:
            raise ParseError(out.get('error') or 'Не удалось извлечь данные из документа', logs)

        mappings = [self._locate(i, record, ocr_pages) for i, record in enumerate(records)]
        image_urls, warnings = self._page_images(parse_id, source_path, work_dir, ocr_pages)

        if all(r.get('extracted_by') == 'rules' for r in records):
            warnings.append('Модель не ответила: поля собраны правилами по координатам.')

        result = result_schema.build_result(
            parse_id=parse_id, filename=filename, records=records, mappings=mappings,
            ocr_pages=ocr_pages, image_urls=image_urls, engine=self.engine,
            logs=logs, warnings=warnings,
        )
        for doc in result['documents']:
            missing = [f['label'] for f in doc['fields'] if not f['boxes']]
            if missing:
                warnings.append('Стр. %d: значение есть, но место на странице не найдено — %s'
                                % (doc['page'], ', '.join(missing)))
        result['warnings'] = warnings

        # Промежуточная чёрно-белая картинка нужна была только для OCR
        try:
            os.remove(os.path.join(work_dir, 'preprocessed.png'))
        except OSError:
            pass
        return result

    def _locate(self, idx, record, ocr_pages):
        """
        Координаты полей одной записи. Логика прежняя (из web_server.py):
        поиск значений по тексту, затем правила — они убирают поля на кодах
        строк и шапке и кладут блоки своих полей прямо из ячеек.
        """
        page_idx = record.get('_page', idx + 1) - 1
        if not 0 <= page_idx < len(ocr_pages):
            page_idx = 0
        page = ocr_pages[page_idx] if ocr_pages else {}
        record['_page'] = page_idx + 1          # координаты относятся к этой странице

        blocks = page.get('blocks', [])
        mapping = build_field_mapping(record, blocks)

        before = dict(record.get('extra_fields') or {})
        self.rule_fields.merge_fields(record, blocks, mapping, grid=page.get('grid'))
        after = record.get('extra_fields') or {}
        by_rules = set(record.get('_rule_fields') or [])
        by_rules |= {k for k, v in after.items() if before.get(k) != v}
        record['_rule_fields'] = [k for k in after if k in by_rules]
        return mapping

    @staticmethod
    def _page_images(parse_id, source_path, work_dir, ocr_pages):
        """
        Картинки страниц рядом с результатом. У PDF их уже сохранил OCR
        (300 dpi, ровно те пиксели, что распознавались). Картинку сохраняем
        сами — пиксели исходника без поворота по EXIF: OCR тоже работал с
        ними, и координаты сходятся один в один.
        """
        urls, warnings = {}, []
        for page in ocr_pages:
            n = page['page']
            target = os.path.join(work_dir, 'page-%d.png' % n)
            if not os.path.isfile(target):
                if source_path.lower().endswith('.pdf'):
                    continue
                with Image.open(source_path) as img:
                    if img.mode not in ('1', 'L', 'LA', 'P', 'RGB', 'RGBA'):
                        img = img.convert('RGB')     # CMYK и прочее PNG не хранит
                    img.save(target, 'PNG')
            with Image.open(target) as img:
                size = img.size
            if size != (page.get('img_width'), page.get('img_height')):
                warnings.append('Стр. %d: размер картинки %sx%s не совпадает с размером '
                                'при распознавании %sx%s' % (n, size[0], size[1],
                                                             page.get('img_width'),
                                                             page.get('img_height')))
            urls[n] = PAGE_IMAGE_URL.format(id=parse_id, n=n)
        return urls, warnings
