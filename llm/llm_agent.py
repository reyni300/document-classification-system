import json
import logging
import time
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


# Сколько всего попыток обращения к модели и пауза между ними.
LLM_MAX_ATTEMPTS = 2
LLM_RETRY_DELAY_SEC = 5


_DECODER = json.JSONDecoder(strict=False)   # strict=False: перенос строки внутри значения


def _skip_ws(text, i):
    while i < len(text) and text[i] in ' \t\r\n':
        i += 1
    return i


def _parse_object_prefix(text, i):
    """
    Читает объект JSON с позиции i (там стоит '{') пара за парой и
    останавливается на первой повреждённой. Возвращает (словарь, позиция,
    объект_дочитан_до_конца).

    Вложенный объект, который сам оборван, не выбрасывается: из него берутся
    целые пары — так в extra_fields остаются поля, записанные до обрыва.
    """
    out = {}
    i = _skip_ws(text, i + 1)
    while i < len(text):
        if text[i] == '}':
            return out, i + 1, True
        try:
            key, i = _DECODER.raw_decode(text, i)
        except ValueError:
            break
        if not isinstance(key, str):
            break
        i = _skip_ws(text, i)
        if i >= len(text) or text[i] != ':':
            break
        i = _skip_ws(text, i + 1)
        if i >= len(text):
            break
        try:
            value, i = _DECODER.raw_decode(text, i)
        except ValueError:
            if text[i] == '{':
                nested, _, _ = _parse_object_prefix(text, i)
                if nested:
                    out[key] = nested
            break
        out[key] = value
        i = _skip_ws(text, i)
        if i < len(text) and text[i] == ',':
            i = _skip_ws(text, i + 1)
            continue
        if i < len(text) and text[i] == '}':
            return out, i + 1, True
        break
    return out, i, False


def parse_json_prefix(text):
    """
    Все целые поля из повреждённого ответа модели, словарём (или None).

    Ответ mistral ломается по-разному: обрывается посреди значения
    ('"vat": nul'), не доходит ни до одной закрывающей скобки, вставляет
    перенос строки внутрь значения, пишет значение без кавычек. Во всех этих
    случаях начало ответа — дата, сумма, валюта, номер, компания — записано
    правильно, а раньше из-за поломки в конце выбрасывался весь ответ.
    Читаем пары по порядку, пока они целые.
    """
    start = text.find('{')
    if start == -1:
        return None
    data, _, _ = _parse_object_prefix(text, start)
    return data or None


def salvage_truncated_json(text):
    """Строка JSON из целых полей повреждённого ответа или None (прежнее имя)."""
    data = parse_json_prefix(text)
    return json.dumps(data, ensure_ascii=False) if data else None


def _is_out_of_memory_error(err):
    """
    Процесс модели снят из-за нехватки памяти. Ollama отдаёт это как 500 с
    текстом "llama-server process has terminated: signal: killed".
    """
    text = str(err).lower()
    return ('signal: killed' in text
            or 'out of memory' in text
            or 'cannot allocate memory' in text
            or 'oom' in text)


def _is_transient_llm_error(err):
    """Ошибка, после которой имеет смысл повторить запрос."""
    text = str(err).lower()
    return (_is_out_of_memory_error(err)
            or 'terminated' in text
            or 'status code: 500' in text
            or 'connection' in text
            or 'timeout' in text
            or 'timed out' in text)


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
        # Причины, по которым не удалось разобрать ответ модели. Нужны, чтобы
        # наверх ушла КОНКРЕТНАЯ ошибка, а не общее "Не удалось извлечь данные
        # из документа": у этого сообщения четыре разные причины (Ollama
        # недоступна, ответ без JSON, сломанный JSON, непрошедшая валидация),
        # и раньше они попадали только в консоль сервера.
        self._last_errors = []

    def _fail(self, reason, page_num):
        """Запоминает причину и логирует её."""
        message = f"стр. {page_num}: {reason}"
        self._last_errors.append(message)
        logging.error(message)
        return None

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
        self._last_errors = []

        if num_pages == 0:
            return []

        if num_pages == 1:
            result = self._extract_from_text(pages_text[0], full_text=pages_text[0], page_num=1)
            final_result = [result] if result and "error" not in result else []
        else:
            # Каждая страница разбирается отдельно и даёт свою запись с НАСТОЯЩИМ
            # номером страницы. Раньше страницу без «№ цифры» склеивали с
            # предыдущей и заменяли её запись общей: у двухстраничного баланса
            # оставалась одна запись на оба листа, вторая страница оставалась
            # без полей, а значения со второй страницы искались на первой.
            # Номер записи был порядковым, а не номером страницы.
            # Страницу, где модель не ответила, оркестратор соберёт правилами.
            all_results = []
            for i, page_text in enumerate(pages_text):
                result = self._extract_from_text(page_text, full_text=page_text, page_num=i + 1)
                if result and "error" not in result:
                    result['_page'] = i + 1
                    result['_total_pages'] = num_pages
                    all_results.append(result)

            final_result = all_results

        # Пустой результат всё равно приведёт к исключению в оркестраторе
        # ("Не удалось извлечь данные из документа"), но по нему невозможно
        # понять, что именно сломалось. Бросаем сами — с конкретной причиной,
        # которая дойдёт до интерфейса.
        if not final_result:
            detail = '; '.join(self._last_errors) if self._last_errors else \
                     'модель не вернула ни одной записи'
            raise RuntimeError(f"LLM не вернул данные ({detail})")

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
- vat: сумма именно НДС (налога на добавленную стоимость) строкой, если есть, иначе null. Это ОТДЕЛЬНЫЙ конкретный налог — не путай его с другими налогами и платежами (например, "налог на прибыль", "налог на доходы организаций"). Если в документе есть другой налог, но НДС не упомянут — оставляй vat: null и НЕ подставляй сюда другой налог.
- document_type: тип документа одним словом или коротко, например: "Кассовый чек", "Банковская квитанция", "Счёт-фактура", "Товарный чек", "Платёжное поручение", "Отчёт о финансовых результатах"

Дополнительное поле extra_fields — словарь с ЛЮБЫМИ важными данными которые ты нашёл в документе.
ГЛАВНОЕ ПРАВИЛО: пройди документ сверху донизу и вынеси в extra_fields КАЖДУЮ строку вида "Название: значение" и КАЖДУЮ строку таблицы — все до одной, не пропуская ни первую, ни последнюю. Плательщик, получатель, банк, назначение платежа, номер карты, адрес, кассир, терминал, номер ЕРИП, код авторизации, RRN, комиссия, сумма операции, показатели отчётности — всё.
Не дублируй в extra_fields основные поля (date, total, currency, invoice_number, company, vat) — КРОМЕ строк таблицы и строк вида "Название: значение": их выписывай ВСЕГДА, даже если такое же число уже попало в total.
Если дополнительных данных нет — extra_fields: {{}}.

ТАБЛИЦЫ со столбцами за разные периоды (например "За 2025 г." и "За 2024 г." рядом для каждого показателя):
- Каждый показатель дай ДВУМЯ отдельными ключами, по одному на период, с годом в названии: "Выручка (2025)" и "Выручка (2024)". Оба периода обязательны, ни один не отбрасывай.
- Никогда не склеивай числа разных периодов в одно значение: два числа подряд у одного показателя — это два разных периода.
- В основные поля (date, total, currency) бери значение только за первый/текущий период.

ПОДПИСЬ в конце документа (пара строк "(подпись)" и "(расшифровка подписи)"):
- Бери ТОЛЬКО расшифровку подписи — напечатанное ФИО целиком, например "Потапов С.Д.". Не бери саму подпись и символы линии ("___", тире). Не дублируй фамилию перед расшифровкой.

Примеры:

Текст: "Чек №456 от 20.01.2024. Магазин Пятёрочка. Итого: 890.00 руб. Кассир: Анна. Товаров: 5"
Вывод: {{"date":"20.01.2024","total":"890.00","currency":"RUB","invoice_number":"456","company":"Пятёрочка","vat":null,"document_type":"Кассовый чек","extra_fields":{{"Кассир":"Анна","Количество товаров":"5"}}}}

Текст: "Приорбанк. Вывоз мусора. Плательщик: Иван Иванов. Сумма 258 BYN. Получатель: Датком Столица. Номер ЕРИП: 7788888. Код авторизации: 542121"
Вывод: {{"date":null,"total":"258.00","currency":"BYN","invoice_number":null,"company":"Датком Столица","vat":null,"document_type":"Банковская квитанция","extra_fields":{{"Плательщик":"Иван Иванов","Банк":"Приорбанк","Услуга":"Вывоз мусора","Номер ЕРИП":"7788888","Код авторизации":"542121"}}}}

Текст: "Отчёт о финансовых результатах за год 2025 г. Организация ООО Кристалл. Единица измерения: тыс. руб. Выручка За 2025 г. 15900 За 2024 г. 13200. Налог на прибыль (доходы) организаций (954) (792). Чистая прибыль (убыток) 726 За 2024 г. 598. Генеральный директор Потапов Потапов С.Д. 01 марта 2026 г."
Вывод: {{"date":"01.03.2026","total":"726","currency":"RUB","invoice_number":null,"company":"ООО Кристалл","vat":null,"document_type":"Отчёт о финансовых результатах","extra_fields":{{"Единица измерения":"тыс. руб.","Выручка (2025)":"15900","Выручка (2024)":"13200","Налог на прибыль (доходы) организаций (2025)":"954","Налог на прибыль (доходы) организаций (2024)":"792","Чистая прибыль (убыток) (2025)":"726","Чистая прибыль (убыток) (2024)":"598","Генеральный директор":"Потапов С.Д."}}}}

Теперь извлеки из следующего текста (страница {page_num}):

{full_text}

Напоминание перед ответом:
1) в extra_fields должна попасть КАЖДАЯ строка "Название: значение" и КАЖДАЯ строка таблицы, включая самую первую и самую последнюю;
2) если у показателя два периода — два отдельных ключа с годом, оба;
3) только JSON, без пояснений.

Вывод (только JSON):
"""

        # Частая беда на машине с небольшим объёмом памяти: ядро убивает
        # llama-server (OOM killer), и Ollama отвечает 500 с текстом вида
        # "llama-server process has terminated: signal: killed". Сам процесс
        # при следующем обращении поднимается заново, поэтому одну повторную
        # попытку делаем сами — иначе обработка документа падает целиком
        # из-за разовой нехватки памяти.
        response = None
        for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
            try:
                response = ollama.chat(
                    model=config['llm']['model'],
                    messages=[{'role': 'user', 'content': prompt}],
                    # format='json' заставляет Ollama декодировать ответ по
                    # грамматике JSON — модель физически не может выдать текст
                    # вокруг объекта или оборвать структуру. Это убирает два из
                    # четырёх видов отказа ("в ответе нет JSON" и "не удалось
                    # разобрать JSON"). В llm_client.py для классификатора то же
                    # самое стоит с самого начала.
                    format='json',
                    options={
                        'temperature': config['llm']['temperature'],
                        # Явный предел длины ответа — чтобы он не зависел от
                        # настроек по умолчанию конкретной версии Ollama.
                        'num_predict': config['llm'].get('num_predict', 1024),
                    }
                )
                break
            except Exception as e:
                if attempt < LLM_MAX_ATTEMPTS and _is_transient_llm_error(e):
                    logging.warning(
                        f"стр. {page_num}: попытка {attempt} не удалась ({e}). "
                        f"Повтор через {LLM_RETRY_DELAY_SEC} c."
                    )
                    time.sleep(LLM_RETRY_DELAY_SEC)
                    continue
                hint = ''
                if _is_out_of_memory_error(e):
                    hint = (' — похоже, процессу модели не хватило оперативной памяти '
                            '(его снял OOM killer). Освободите память или возьмите '
                            'модель полегче.')
                return self._fail(
                    f"Ollama недоступна или вернула ошибку: {e}{hint}", page_num)

        if response is None:
            return self._fail("Ollama не вернула ответ", page_num)

        content = response['message']['content']
        print(f"[LLM RAW OUTPUT page {page_num}]: {content[:200]}...")

        first_brace = content.find('{')
        if first_brace == -1:
            return self._fail(
                f"в ответе модели нет JSON (получено {len(content)} симв.: "
                f"{content[:120]!r})", page_num)

        data = None
        last_brace = content.rfind('}')
        if last_brace > first_brace:
            try:
                data = json.loads(clean_json_trailing_commas(content[first_brace:last_brace + 1]),
                                  strict=False)
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            # Ответ повреждён (оборван, лишний текст, значение без кавычек) —
            # берём все целые поля с начала. Полный ответ пишем в лог
            # сервиса: по нему видно, что именно сломалось.
            data = parse_json_prefix(content)
            print(f"[LLM FULL OUTPUT page {page_num}] ({len(content)} симв.): {content}")
            if not data:
                return self._fail(
                    f"не удалось разобрать JSON из ответа модели ({content[:120]!r})", page_num)
            print(f"[LLM] стр. {page_num}: ответ модели повреждён, взято целых полей: {len(data)}")

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
            return self._fail(f"данные не прошли валидацию: {e}", page_num)

    def interpret(self, ocr_json):
        result = self.interpret_multi(ocr_json)
        if result and len(result) > 0:
            return result[0]
        return {"error": "No data extracted"}
