FROM python:3.14-alpine3.24 AS builder

WORKDIR /build

COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.14-alpine3.24

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DB_PATH=/data/data.db

RUN addgroup -S app && adduser -S -G app app && mkdir -p /data && chown app:app /data

COPY --from=builder /opt/venv /opt/venv
COPY app.py ./
COPY access_gate_service.py ./
COPY contact_service.py ./
COPY db.py ./
COPY quiz.py ./
COPY question_repository.py ./
COPY metar.py ./
COPY taf.py ./
COPY system_status.py ./
COPY translations.py ./
COPY gunicorn_config.py ./
COPY docker-entrypoint.sh ./
COPY robots.txt ./
COPY sitemap.xml ./
COPY static ./static
COPY templates ./templates
COPY data.db ./data.db

RUN chmod +x /app/docker-entrypoint.sh && chown -R app:app /app /data

USER app

EXPOSE 5000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn_config.py", "app:app"]
