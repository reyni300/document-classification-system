# Используем официальный образ Python
FROM python:3.12-slim

# Устанавливаем системные зависимости для Ubuntu, Tkinter и Tesseract OCR
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libxcb-xinerama0 \
    libqt5gui5 \
    libxrender1 \
    libxtst6 \
    libxi6 \
    python3-tk \
    tk-dev \
    tesseract-ocr \
    tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

# Создаем рабочую директорию внутри контейнера
WORKDIR /app

# Копируем файл с зависимостями и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все остальные файлы проекта в контейнер
COPY . .

# Команда для запуска приложения
CMD ["python", "main.py"]
