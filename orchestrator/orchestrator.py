import logging
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from preprocessing.preprocessor import preprocess_image
from translation.translator import normalize_data

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

    def process_document(self, file_path, filename):
        logs = []
        doc_id = None

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
            for i, page in enumerate(ocr_data['pages']):
                if self.translator:
                    page['text'] = self.translator(page['text'])
                else:
                    page['text'] = normalize_data(page['text'])

            logs.append("ШАГ 5: LLM интерпретация (извлечение полей)...")
            parsed_list = self.llm.interpret_multi(ocr_data)
            logs.append(f"Извлечено записей: {len(parsed_list)}")

            if not parsed_list:
                raise ValueError("Не удалось извлечь данные из документа")

            for i, page in enumerate(ocr_data['pages']):
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
                enriched_parsed_list.append(enriched)

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
                "ocr_text_preview": full_ocr_text[:500]
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
                "parsed_list": []
            }