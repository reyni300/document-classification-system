# Сервис разбора. Собирается из корня репозитория:
#   docker compose build parser
FROM python:3.12-slim

# Tesseract с русским языком, poppler для PDF, библиотеки для OpenCV
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-rus \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY parser_service/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Только то, что нужно для разбора: веб-интерфейса, базы и GUI здесь нет
COPY config.yaml rule_fields.py dump_blocks.py ./
COPY agents ./agents
COPY llm ./llm
COPY ocr ./ocr
COPY orchestrator ./orchestrator
COPY preprocessing ./preprocessing
COPY translation ./translation
COPY parser_service ./parser_service
COPY tools ./tools

ENV PYTHONUNBUFFERED=1 \
    PARSER_DATA_DIR=/app/data/parses \
    PARSER_PORT=8000

EXPOSE 8000
CMD ["python", "-m", "parser_service"]
