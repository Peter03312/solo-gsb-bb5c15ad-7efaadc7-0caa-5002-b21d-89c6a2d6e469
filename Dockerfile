FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --system --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /srv/app
USER appuser

ENV DATABASE=/data/blind.db \
    SECRET_KEY=change-me-in-production \
    DEFAULT_SEED=20260101 \
    MIN_REASON_LENGTH=10

VOLUME ["/data"]
EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "run:app"]
