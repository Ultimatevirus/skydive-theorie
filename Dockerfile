FROM python:3.12-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/vragen.db

RUN mkdir -p /data

COPY app.py ./
COPY gunicorn_config.py ./
COPY static ./static
COPY templates ./templates
COPY vragen.db /data/vragen.db

RUN pip install --upgrade pip
RUN pip install --no-cache-dir flask
RUN pip install --no-cache-dir gunicorn

EXPOSE 5000

CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]
