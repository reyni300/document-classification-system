# Обработчик финансовых документов

Desktop-приложение на Python (Tkinter) для распознавания, извлечения и классификации данных из финансовых документов (счета, чеки, накладные) с помощью OCR и LLM.

Поддерживаемые форматы: **PDF / PNG / JPG / JPEG**

## Возможности

- загрузка PDF или изображения через графический интерфейс с предпросмотром
- предобработка изображения (адаптивная бинаризация, усиление контраста, поворот)
- распознавание текста через Tesseract OCR (русский + английский)
- автоматическое извлечение полей через LLM (Ollama + mistral): дата, сумма, валюта, номер счёта, компания, НДС
- классификация расхода по 50+ категориям (еда, транспорт, связь, развлечения и т.д.)
- валидация данных: проверка суммы, даты, валюты, отсутствия отрицательных цен
- сохранение всех документов в SQLite с историей
- просмотр истории, удаление, экспорт в JSON
- логирование всех этапов обработки

## Требования

- Windows / Linux / macOS
- Python 3.9+
- Tesseract OCR (с русским языком)
- Ollama (с моделью mistral)
- Poppler (для PDF)

## Установка и запуск

```bash
#1. Клонирование репозитория 
git clone https://github.com/DudichKoptevichTsalko/document-classification-system.git 

# 2. Создать виртуальное окружение
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
.venv\Scripts\activate     # Windows

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Установить внешние зависимости
Tesseract 
- (Windows - скачать с https://github.com/UB-Mannheim/tesseract/wiki)
- Linux: sudo apt-get install tesseract-ocr tesseract-ocr-rus
- macOS: brew install tesseract tesseract-lang

Poppler 
- (Windows - скачать с https://github.com/oschwartz10612/poppler-windows/releases)
- Linux: sudo apt-get install poppler-utils
- macOS: brew install poppler

Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama pull mistral

# 5. Запустить приложение
python main.py