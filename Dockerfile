FROM python:3.12-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/vragen.db

RUN mkdir -p /data

COPY app.py ./
COPY static ./static
COPY templates ./templates
COPY vragen.db /data/vragen.db

RUN pip install --no-cache-dir flask

EXPOSE 5000

CMD ["python", "app.py"]
