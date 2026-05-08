import re
from deep_translator import GoogleTranslator


def normalize_data(text):
    if not text:
        return ""

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
    if not text:
        return ""
    try:
        translated = GoogleTranslator(source="auto", target="ru").translate(text)
        return normalize_data(translated)
    except:
        return normalize_data(text)