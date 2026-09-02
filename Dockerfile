FROM python:3.14-alpine3.24

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/vragen.db

RUN mkdir -p /data

COPY app.py ./
COPY translations.py ./
COPY gunicorn_config.py ./
COPY static ./static
COPY templates ./templates
COPY vragen.db /data/vragen.db

RUN pip install --no-cache-dir --upgrade pip \
    flask \
    gunicorn

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]
