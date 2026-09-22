import logging
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from preprocessing.preprocessor import preprocess_image
from translation.translator import normalize_data
import rule_fields

import yaml

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


class Orchestrator:
    def __init__(self, ocr_module, llm_agent, db_module, classifier, validator,
                 translator_func=None):
        self.ocr = ocr_module
        self.llm = llm_agent
        self.db = db_module
        self.classifier = classifier
        self.validator = validator
        self.translator = translator_func

    def _parse_by_rules(self, ocr_data, logs):
        """
        Запасной разбор, когда LLM не ответила: поля берутся только правилами
        по координатам OCR-блоков (rule_fields). Страницы, где правила ничего
        не нашли, пропускаем.
        """
        pages = ocr_data.get('pages', []) if ocr_data else []
        by_rules = []
        for i, page in enumerate(pages):
            item = {
                '_page': i + 1,
                'total': None, 'date': None, 'company': None,
                'invoice_number': None, 'vat': None, 'currency': None,
                'document_type': 'документ',
                'extra_fields': {},
                'extracted_by': 'rules',
            }
            found = rule_fields.merge_fields(item, page.get('blocks', []),
                                              grid=page.get('grid'))
            logs.append(f"  Стр. {i + 1}: правила нашли полей — {found}")
            if found:
                by_rules.append(item)
        return by_rules

    def process_document(self, file_path, filename):
        logs = []
        doc_id = None
        llm_failure = None
        ocr_data = None  # используется и в ветке успеха, и в except — для отдачи блоков наверх

        try:
            logs.append(f"НАЧАЛО ОБРАБОТКИ: {filename}")

            logs.append("ШАГ 1: Предобработка изображения...")
            processed_path = preprocess_image(file_path) if file_path.lower().endswith(
                ('.png', '.jpg', '.jpeg')) else file_path
            logs.append("Предобработка завершена")

            logs.append("ШАГ 2: Распознавание текста (OCR)...")
            ocr_data = self.ocr.extract(processed_path if processed_path else file_path)

            full_ocr_text = "\n\n--- РАЗРЫВ СТРАНИЦЫ ---\n\n".join(
                page['text'] for page in ocr_data['pages']
            )

            logs.append(f"OCR завершён. Символов: {len(full_ocr_text)}")
            logs.append(f"OCR движок: {ocr_data['ocr_engine']}")
            logs.append(f"Страниц: {len(ocr_data['pages'])}")

            logs.append("ШАГ 3: Нормализация текста (даты, валюты)...")
            normalized_text = normalize_data(full_ocr_text)

            translated_text = normalized_text
            if self.translator:
                logs.append("ШАГ 4: Перевод на русский...")
                translated_text = self.translator(normalized_text)
                logs.append("Перевод выполнен")
            else:
                logs.append("ШАГ 4: Перевод отключён")

            original_pages_text = [page['text'] for page in ocr_data['pages']]

            # Нормализуем OCR-артефакты валют прямо в тексте страниц до подачи в LLM
            currency_ocr_fixes = {
                r'\bВУМ\b': 'BYN', r'\bВУН\b': 'BYN', r'\bBУМ\b': 'BYN',
                r'\bВВМ\b': 'BYN', r'\bBYR\b': 'BYN', r'\bBUM\b': 'BYN',
            }
            for i, page in enumerate(ocr_data['pages']):
                text = page['text']
                for pattern, replacement in currency_ocr_fixes.items():
                    text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
                if self.translator:
                    page['text'] = self.translator(text)
                else:
                    page['text'] = normalize_data(text)

            logs.append("ШАГ 5: LLM интерпретация (извлечение полей)...")
            # interpret_multi бросает RuntimeError, если модель не дала ничего.
            # Раньше это была фатальная ошибка; теперь — повод включить правила.
            try:
                parsed_list = self.llm.interpret_multi(ocr_data)
                logs.append(f"Извлечено записей: {len(parsed_list)}")
            except Exception as llm_error:
                llm_failure = str(llm_error)
                logs.append(f"LLM не отработала: {llm_failure}")
                parsed_list = []

            if not parsed_list:
                # Модель молчит или вернула мусор. Раньше здесь обработка
                # обрывалась и пользователь не получал НИЧЕГО. Но поля по
                # координатам OCR-блоков модель не нужна — собираем документ
                # правилами и идём дальше.
                logs.append("Модель данных не дала — пробуем собрать поля правилами...")
                parsed_list = self._parse_by_rules(ocr_data, logs)
                if not parsed_list:
                    raise ValueError(llm_failure or "Не удалось извлечь данные из документа")
                logs.append(f"Документ собран правилами, без модели: записей {len(parsed_list)}")

            for i, page in enumerate(ocr_data['pages']):
                if i < len(original_pages_text):
                    page['text'] = original_pages_text[i]

            logs.append("ШАГ 6: Классификация и валидация...")
            enriched_parsed_list = []
            for idx, parsed in enumerate(parsed_list):
                data_for_classifier = {
                    "merchant": parsed.get('company', ''),
                    "items": [],
                    "total": parsed.get('total', 0),
                    "currency": parsed.get('currency', 'RUB'),
                    "date": parsed.get('date', '')
                }
                category_result = self.classifier.classify(data_for_classifier)

                if config['app'].get('enable_validation', True):
                    total_value = 0.0
                    if parsed.get('total'):
                        try:
                            total_str = str(parsed.get('total')).replace(',', '.')
                            numbers = re.findall(r'[\d\.]+', total_str)
                            if numbers:
                                total_value = float(numbers[0])
                        except:
                            total_value = 0.0

                    date_value = parsed.get('date', '') or ''
                    currency_value = parsed.get('currency', 'RUB') or 'RUB'

                    validation_data = {
                        "items": [],
                        "total": total_value,
                        "currency": currency_value,
                        "date": date_value
                    }
                    validation = self.validator.validate(validation_data)
                else:
                    validation = {"valid": True, "errors": []}

                enriched = parsed.copy()
                enriched['category'] = category_result.get('category', 'прочее')
                enriched['validation_valid'] = validation['valid']
                if validation.get('errors'):
                    enriched['validation_errors'] = validation['errors']
                # Поля, которые модель не вернула, добираем правилами по координатам
                # OCR-блоков. Делаем это ДО сохранения в БД, иначе в историю попал бы
                # неполный набор полей, а на экране сразу после обработки — полный.
                page_idx = parsed.get('_page', idx + 1) - 1
                pages = ocr_data['pages']
                страница = (pages[page_idx] if 0 <= page_idx < len(pages)
                            else (pages[0] if pages else {}))
                page_blocks = страница.get('blocks', [])
                by_rules = rule_fields.merge_fields(enriched, page_blocks,
                                                    grid=страница.get('grid'))

                enriched_parsed_list.append(enriched)

                logs.append(f"  Запись {idx + 1}: полей по правилам={by_rules}")
                logs.append(
                    f"  Запись {idx + 1}: категория={enriched['category']}, валидация={'ПРОЙДЕНА' if validation['valid'] else 'НЕ ПРОЙДЕНА'}")

            logs.append("ШАГ 7: Сохранение в базу данных...")
            ocr_version = ocr_data['ocr_engine']
            llm_version = config['llm'].get('version', config['llm']['model'])

            doc_id = self.db.save(
                filename=filename,
                ocr_text=full_ocr_text,
                parsed_data=enriched_parsed_list,
                ocr_version=ocr_version,
                llm_version=llm_version
            )

            self.db.log_event(doc_id, "OCR_COMPLETE", f"Engine: {ocr_version}")
            self.db.log_event(doc_id, "LLM_PARSE", f"Извлечено {len(parsed_list)} записей")
            self.db.log_event(doc_id, "MULTI_PAGE", f"Документ содержит {len(parsed_list)} страниц/счетов")

            for log_msg in logs:
                self.db.log_event(doc_id, "ОРКЕСТРАТОР", log_msg)

            logs.append(f"Сохранено в БД с ID: {doc_id}")
            logs.append("ОБРАБОТКА ЗАВЕРШЕНА УСПЕШНО")

            return {
                "doc_id": doc_id,
                "filename": filename,
                "parsed_list": enriched_parsed_list,
                "logs": logs,
                "ocr_text_preview": full_ocr_text[:500],
                # Отдаём наверх те же самые страницы/блоки, на которых реально считал LLM —
                # чтобы web_server.py строил маппинг полей по НИМ, а не гонял OCR второй раз
                # по другому варианту изображения (см. комментарий в web_server.py)
                "ocr_pages": ocr_data['pages'] if ocr_data else []
            }

        except Exception as e:
            error_msg = f"КРИТИЧЕСКАЯ ОШИБКА: {str(e)}"
            logs.append(error_msg)
            logging.error(error_msg)

            if doc_id:
                self.db.log_event(doc_id, "ОШИБКА", error_msg)

            return {
                "doc_id": None,
                "filename": filename,
                "error": str(e),
                "logs": logs,
                "parsed_list": [],
                "ocr_pages": ocr_data['pages'] if ocr_data else []
            }