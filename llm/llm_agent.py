import json
import logging
import re
import yaml
import ollama
from pydantic import BaseModel, validator
from typing import Optional

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
        if v_str in ['РУБ', 'Р', 'РУБЛЬ', 'РУБЛЕЙ']:
            return 'RUB'
        if v_str in ['USD', '$', 'ДОЛЛ', 'ДОЛЛАР']:
            return 'USD'
        if v_str in ['EUR', '€', 'ЕВРО']:
            return 'EUR'
        if v_str in ['BYN', 'БЕЛ', 'БЕЛРУБ', 'Б.Р.']:
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
        v_str = v_str.replace('`', "'").replace('‘', "'").replace('’', "'")
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


class LLMAgent:
    def interpret(self, ocr_json):
        # Объединяем текст со всех страниц
        full_text = "\n\n".join(page['text'] for page in ocr_json['pages'])

        # Добавляем информацию о количестве страниц для контекста
        if len(ocr_json['pages']) > 1:
            full_text = f"[Документ содержит {len(ocr_json['pages'])} страниц]\n\n{full_text}"

        # В методе interpret замените prompt на русскоязычный:

        prompt = f"""
        Ты — строгий экстрактор данных из финансовых документов. Верни ТОЛЬКО валидный JSON. Без пояснений. Извлекай ТОЛЬКО следующие поля: date, total, currency, invoice_number, company, vat. Не добавляй другие поля.

        Важные указания:
        - date: дата в формате ДД.ММ.ГГГГ (день.месяц.год)
        - total: итоговая сумма (строка, например "12000.50")
        - currency: код валюты (RUB, USD, EUR, BYN)
        - invoice_number: номер счёта или чека
        - company: название организации или магазина
        - vat: сумма НДС (если есть), иначе пустая строка

        Примеры правильного извлечения:

        Пример 1:
        Текст: "Счёт № 123 от 15.03.2024. Сумма: 5000.50 руб., НДС 1000.10 руб. Поставщик: ООО Ромашка"
        Вывод: {{"date": "15.03.2024", "total": "5000.50", "currency": "RUB", "invoice_number": "123", "company": "ООО Ромашка", "vat": "1000.10"}}

        Пример 2:
        Текст: "Счет-фактура № 789 от 12.11.2024 г. Итого к оплате: 120 000,00 рублей, в том числе НДС 20 000,00. Исполнитель: ООО Канапе"
        Вывод: {{"date": "12.11.2024", "total": "120000.00", "currency": "RUB", "invoice_number": "789", "company": "ООО Канапе", "vat": "20000.00"}}

        Пример 3 (без НДС):
        Текст: "Чек № 952993971 от 2021.06.15 17:46:11, сумма 60 BYN, магазин 'Друг садовода'"
        Вывод: {{"date": "2021.06.15", "total": "60.00", "currency": "BYN", "invoice_number": "952993971", "company": "Друг садовода", "vat": ""}}

        Теперь извлеки из следующего текста. Если какое-то поле отсутствует, верни пустую строку.

        Текст:
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
            logging.error(f"Ollama error: {e}")
            return {"error": "LLM request failed", "raw": str(e)}

        content = response['message']['content']
        start = content.find('{')
        end = content.rfind('}') + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON object found in response")
        json_str = content[start:end]
        json_str = clean_json_trailing_commas(json_str)

        try:
            data = json.loads(json_str)
            validated = InvoiceData(**data)
            result = validated.dict()

            if not result.get('invoice_number'):
                match = re.search(r'(?:№|Номер)\s*[:]?\s*([0-9\-]+)', full_text)
                if match:
                    result['invoice_number'] = match.group(1)

            if not result.get('company'):
                patterns = [
                    r'(?:Поставщик|Исполнитель|Продавец)\s*[:]?\s*([А-Яа-я0-9\s\-\"\'\,\(\)]+?)(?:\n|,|$)',
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

        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"JSON/decode error: {e}\nRaw: {json_str}")
            return {"error": "Invalid JSON", "raw": json_str}