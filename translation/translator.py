import re
from deep_translator import GoogleTranslator


def normalize_data(text):
    if not text:
        return ""

    # === НАЧАЛО БЛОКА: ИСПРАВЛЕНИЕ OCR-ОШИБОК ВАЛЮТ ===
    # Очищаем строку от пробелов и переводим в верхний регистр для точного сравнения
    cleaned_currency = str(text).strip().upper()
    
    # Если Tesseract перепутал буквы и прочитал BYN как ВУМ/VUM/БУН/ВУН, 
    # мы возвращаем чистый, правильный ISO-код BYN
    if cleaned_currency in ['ВУМ', 'VUM', 'БУН', 'BYM', 'ВУН', 'BYN']:
        return 'BYN'
        
    # То же самое делаем для других стандартных валют на случай лишних пробелов
    if cleaned_currency in ['RUB', 'РУБ', 'РУБ.']:
        return 'RUB'
    if cleaned_currency in ['USD', 'EUR', 'KZT']:
        return cleaned_currency
    # === КОНЕЦ БЛОКА ===

    # ТВОЙ СТАРЫЙ КОД (ОСТАЛСЯ ПОЛНОСТЬЮ БЕЗ ИЗМЕНЕНИЙ)
    text = text.replace("долларов США", "$").replace("доллара", "$").replace("доллар", "$")
    text = text.replace("евро", "€")

    months = {
        "января": "01", "февраля": "02", "марта": "03", "апреля": "04",
        "мая": "05", "июня": "06", "июля": "07", "августа": "08",
        "сентября": "09", "октября": "10", "ноября": "11", "декабря": "12",
    }
    for name, num in months.items():
        text = re.sub(rf"(\d{{1,2}})\s{name}\s(\d{{4}})", rf"\1.{num}.\2", text, flags=re.IGNORECASE)

    date_pattern = r"(\d{2,4})[-/](\d{2})[-/](\d{2,4})"

    def date_fixer(match):
        g1, g2, g3 = match.groups()
        if len(g1) == 4:
            return f"{g3}.{g2}.{g1}"
        return f"{g1}.{g2}.{g3}"

    return re.sub(date_pattern, date_fixer, text)


def translate_field(text):
    print(r"=== ВХОД В ПЕРЕВОДЧИК ===", repr(text))  # Покажет, что прислал ИИ
    
    if not text:
        return ""
        
    cleaned_text = str(text).strip().upper()
    ISO_CURRENCIES = ['USD', 'EUR', 'BYN', 'RUB', 'KZT', 'UAH']
    
    if cleaned_text in ISO_CURRENCIES:
        print(r"=== СРАБОТАЛ ФИЛЬТР ВАЛЮТ ===", cleaned_text)
        return cleaned_text

    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(text)
        res = normalize_data(translated)
        print(r"=== ВЫХОД ИЗ ПЕРЕВОДЧИКА (УСПЕХ) ===", repr(res))
        return res
    except:
        res = normalize_data(text)
        print(r"=== ВЫХОД ИЗ ПЕРЕВОДЧИКА (ОШИБКА) ===", repr(res))
        return res
