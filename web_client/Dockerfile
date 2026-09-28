# Веб-интерфейс. Собирается из папки web_client:
#   docker compose build web
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY . ./web_client

ENV PYTHONUNBUFFERED=1 \
    WEB_PORT=5000 \
    PARSER_URL=http://parser:8000

EXPOSE 5000
CMD ["python", "-m", "web_client"]
