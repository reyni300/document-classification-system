import json
import logging
import re
import yaml
import ollama
from pydantic import BaseModel, validator
from typing import Optional, Dict

with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)


def clean_json_trailing_commas(json_str):
    json_str = re.sub(r',\s*}', '}', json_str)
    json_str = re.sub(r',\s*]', ']', json_str)
    return json_str


class InvoiceData(BaseModel):
    date: Optional[str] = None
    total: Optional[str] = None
    currency: str = "RUB"
    invoice_number: Optional[str] = None
    company: Optional[str] = None
    vat: Optional[str] = None
    document_type: Optional[str] = None      # тип документа (чек, квитанция, счёт и т.д.)
    extra_fields: Optional[Dict] = None      # любые дополнительные поля которые нашёл LLM

    @validator('total', pre=True)
    def normalize_total(cls, v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return f"{v:.2f}"
        v_str = str(v).strip()
        v_str = v_str.replace(' ', '').replace(',', '.')
        v_str = re.sub(r'[^\d.-]', '', v_str)
        try:
            return f"{float(v_str):.2f}"
        except ValueError:
            return None

    @validator('date', pre=True)
    def normalize_date(cls, v):
        if v is None:
            return None
        v_str = str(v).strip()
        if ' ' in v_str:
            v_str = v_str.split()[0]
        v_str = v_str.replace(' г.', '').strip()
        return v_str

    @validator('currency', pre=True)
    def normalize_currency(cls, v):
        if v is None or v == '':
            return 'RUB'
        v_str = str(v).strip().upper()

        if v_str in ['RUB', 'USD', 'EUR', 'BYN']:
            return v_str
        if v_str in ['РУБ', 'Р', 'РУБЛЬ', 'РУБЛЕЙ', 'RUR']:
            return 'RUB'
        if v_str in ['$', 'ДОЛЛ', 'ДОЛЛАР', 'DOLLARS']:
            return 'USD'
        if v_str in ['€', 'ЕВРО', 'EURO']:
            return 'EUR'
        if v_str in [
            'BYN', 'BYR',
            'ВУМ', 'ВУН', 'BУМ', 'BУН',
            'VUM', 'VUN', 'BUM', 'BUN',
            'БЕЛ', 'БЕЛРУБ', 'Б.Р.', 'БР',
            'БЕЛОРУССКИЙ', 'BEL',
        ]:
            return 'BYN'
        return 'RUB'

    @validator('invoice_number', pre=True)
    def normalize_number(cls, v):
        if v is None:
            return ''
        v_str = str(v).strip()
        v_str = re.sub(r'[^\w\-]', '', v_str)
        return v_str

    @validator('company', pre=True)
    def normalize_company(cls, v):
        if v is None:
            return ''
        v_str = str(v).strip()
        v_str = v_str.replace('"', '').replace('\n', ' ').strip()
        v_str = re.sub(r'0+', 'О', v_str)
        v_str = v_str.replace('`', "'").replace('\u2018', "'").replace('\u2019', "'")
        v_str = v_str.strip("'")
        return v_str

    @validator('vat', pre=True)
    def normalize_vat(cls, v):
        if v is None or v == '':
            return ''
        v_str = str(v).strip()
        v_str = re.sub(r'НДС\s*', '', v_str, flags=re.IGNORECASE)
        v_str = v_str.replace(' ', '').replace(',', '.')
        v_str = re.sub(r'[^\d.-]', '', v_str)
        try:
            return f"{float(v_str):.2f}"
        except:
            return v_str

    @validator('document_type', pre=True)
    def normalize_document_type(cls, v):
        if v is None:
            return None
        return str(v).strip()

    @validator('extra_fields', pre=True)
    def normalize_extra_fields(cls, v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            return {}

        # Словарь нормализации валют — те же варианты что и в normalize_currency
        currency_map = {
            'ВУМ': 'BYN', 'ВУН': 'BYN', 'BУМ': 'BYN', 'BУН': 'BYN',
            'VUM': 'BYN', 'VUN': 'BYN', 'BUM': 'BYN', 'BUN': 'BYN',
            'БЕЛ': 'BYN', 'БЕЛРУБ': 'BYN', 'Б.Р.': 'BYN', 'БР': 'BYN',
            'БЕЛОРУССКИЙ': 'BYN', 'BEL': 'BYN', 'BYR': 'BYN',
            'РУБ': 'RUB', 'РУБЛЬ': 'RUB', 'РУБЛЕЙ': 'RUB', 'RUR': 'RUB',
        }

        def fix_currency_in_value(val_str):
            """Исправляет OCR-артефакты валюты внутри строки значения."""
            for wrong, correct in currency_map.items():
                # Заменяем как отдельное слово (с границами)
                val_str = re.sub(
                    r'(?<![А-Яа-яA-Za-z])' + re.escape(wrong) + r'(?![А-Яа-яA-Za-z])',
                    correct, val_str
                )
            return val_str

        cleaned = {}
        for key, val in v.items():
            if val is None:
                continue
            val_str = str(val).strip()
            if val_str in ('', 'N/A', 'null', 'None', '-'):
                continue
            val_str = fix_currency_in_value(val_str)
            cleaned_key = str(key).strip().replace('_', ' ').capitalize()
            cleaned[cleaned_key] = val_str
        return cleaned


class LLMAgent:
    def __init__(self):
        self._last_ocr_hash = None
        self._last_result = None

    def _get_ocr_hash(self, ocr_json):
        import hashlib
        text = ''.join([page['text'][:100] for page in ocr_json['pages']])
        return hashlib.md5(text.encode()).hexdigest()

    def interpret_multi(self, ocr_json):
        pages_text = [page['text'] for page in ocr_json['pages']]
        num_pages = len(pages_text)

        current_hash = self._get_ocr_hash(ocr_json)
        if self._last_ocr_hash == current_hash:
            print(f"DEBUG: Возвращаем кэшированный результат для {num_pages} страниц")
            return self._last_result

        print(f"DEBUG interpret_multi: {num_pages} страниц")

        if num_pages == 0:
            return []

        if num_pages == 1:
            result = self._extract_from_text(pages_text[0], full_text=pages_text[0], page_num=1)
            final_result = [result] if result and "error" not in result else []
        else:
            all_results = []
            for i, page_text in enumerate(pages_text):
                has_invoice = re.search(r'СЧЕТ\s*НА\s*ОПЛАТУ|Счет\s*№|№\s*\d+', page_text, re.IGNORECASE)
                if has_invoice:
                    result = self._extract_from_text(page_text, full_text=page_text, page_num=i + 1)
                    if result and "error" not in result:
                        all_results.append(result)
                else:
                    if all_results and i > 0:
                        combined_text = pages_text[i - 1] + "\n\n" + page_text
                        result = self._extract_from_text(combined_text, full_text=combined_text, page_num=i)
                        if result and "error" not in result and len(all_results) > 0:
                            all_results[-1] = result

            if not all_results:
                combined_text = "\n\n".join(pages_text)
                result = self._extract_from_text(combined_text, full_text=combined_text, page_num=1)
                if result and "error" not in result:
                    all_results = [result]

            for i, result in enumerate(all_results):
                result['_page'] = i + 1
                result['_total_pages'] = len(all_results)

            final_result = all_results

        self._last_ocr_hash = current_hash
        self._last_result = final_result
        print(f"DEBUG: Извлечено {len(final_result)} записей из {num_pages} страниц")
        return final_result

    def _extract_from_text(self, page_text, full_text=None, page_num=1):
        if full_text is None:
            full_text = page_text

        prompt = f"""
Ты — экстрактор данных из финансовых документов. Верни ТОЛЬКО валидный JSON. Без пояснений, без markdown.

Обязательные поля (если не найдено — null):
- date: дата документа в формате ДД.ММ.ГГГГ
- total: итоговая сумма строкой, например "258.00"
- currency: код валюты — ТОЛЬКО одно из: RUB, USD, EUR, BYN
- invoice_number: номер чека, счёта, квитанции
- company: название организации, магазина или получателя платежа
- vat: сумма НДС строкой, если есть, иначе null
- document_type: тип документа одним словом или коротко, например: "Кассовый чек", "Банковская квитанция", "Счёт-фактура", "Товарный чек", "Платёжное поручение"

Дополнительное поле extra_fields — словарь с ЛЮБЫМИ важными данными которые ты нашёл в документе.
Включай в extra_fields всё значимое: плательщик, получатель, банк, назначение платежа, номер карты, номер телефона, адрес, артикулы товаров, количество позиций, скидка, бонусы, кассир, терминал, номер ЕРИП, код авторизации и т.д.
НЕ включай в extra_fields то что уже есть в основных полях (date, total, currency, invoice_number, company, vat).
Если дополнительных данных нет — extra_fields: {{}}.

Примеры:

Текст: "Чек №456 от 20.01.2024. Магазин Пятёрочка. Итого: 890.00 руб. Кассир: Анна. Товаров: 5"
Вывод: {{"date":"20.01.2024","total":"890.00","currency":"RUB","invoice_number":"456","company":"Пятёрочка","vat":null,"document_type":"Кассовый чек","extra_fields":{{"Кассир":"Анна","Количество товаров":"5"}}}}

Текст: "Приорбанк. Вывоз мусора. Плательщик: Иван Иванов. Сумма 258 BYN. Получатель: Датком Столица. Номер ЕРИП: 7788888. Код авторизации: 542121"
Вывод: {{"date":null,"total":"258.00","currency":"BYN","invoice_number":null,"company":"Датком Столица","vat":null,"document_type":"Банковская квитанция","extra_fields":{{"Плательщик":"Иван Иванов","Банк":"Приорбанк","Услуга":"Вывоз мусора","Номер ЕРИП":"7788888","Код авторизации":"542121"}}}}

Теперь извлеки из следующего текста (страница {page_num}):

{full_text}

Вывод (только JSON):
"""

        try:
            response = ollama.chat(
                model=config['llm']['model'],
                messages=[{'role': 'user', 'content': prompt}],
                options={'temperature': config['llm']['temperature']}
            )
        except Exception as e:
            logging.error(f"Ollama error on page {page_num}: {e}")
            return None

        content = response['message']['content']
        print(f"[LLM RAW OUTPUT page {page_num}]: {content[:200]}...")

        first_brace = content.find('{')
        last_brace = content.rfind('}')
        if first_brace == -1 or last_brace == -1:
            logging.error(f"No JSON found on page {page_num}")
            return None

        json_str = content[first_brace:last_brace + 1]
        json_str = clean_json_trailing_commas(json_str)

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            objects = []
            brace_count = 0
            start_idx = -1
            for i, ch in enumerate(content):
                if ch == '{':
                    if brace_count == 0:
                        start_idx = i
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0 and start_idx != -1:
                        obj_str = content[start_idx:i + 1]
                        try:
                            obj_str_cleaned = clean_json_trailing_commas(obj_str)
                            objects.append(json.loads(obj_str_cleaned))
                        except:
                            pass
                        start_idx = -1
            if objects:
                data = objects[0]
            else:
                logging.error(f"Could not parse JSON on page {page_num}")
                return None

        try:
            validated = InvoiceData(**data)
            result = validated.dict()

            # Фолбэки если LLM не нашёл — ищем регулярками
            if not result.get('invoice_number'):
                match = re.search(r'(?:№|Номер)\s*[:]?\s*([0-9\-]+)', full_text)
                if match:
                    result['invoice_number'] = match.group(1)

            if not result.get('company'):
                patterns = [
                    r'(?:Поставщик|Исполнитель|Продавец|Получатель платежа)\s*[:]?\s*([А-Яа-я0-9\s\-\"\'\,\(\)]+?)(?:\n|,|$)',
                    r'(?:ООО|ЗАО|АО|ИП|ЧУП|ОАО|ПАО)\s+[А-Яа-я0-9\s\-\"\']+',
                ]
                for pat in patterns:
                    match = re.search(pat, full_text, re.IGNORECASE)
                    if match:
                        company_candidate = match.group(1) if match.lastindex else match.group(0)
                        company_candidate = company_candidate.strip().strip('"\'')
                        if len(company_candidate) > 2:
                            result['company'] = company_candidate
                            break

            if not result.get('vat'):
                match = re.search(r'НДС\s*[:]?\s*([0-9\.,]+)', full_text, re.IGNORECASE)
                if match:
                    vat_str = match.group(1).replace(',', '.')
                    result['vat'] = vat_str

            return result

        except Exception as e:
            logging.error(f"Validation error on page {page_num}: {e}")
            return None

    def interpret(self, ocr_json):
        result = self.interpret_multi(ocr_json)
        if result and len(result) > 0:
            return result[0]
        return {"error": "No data extracted"}
