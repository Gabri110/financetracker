# FinanceTracker Docker image (single user, data on the /data volume)

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE=/data/finance.db \
    DATA_DIR=/data \
    SECRET_KEY_FILE=/data/secret_key

# Non-root user
RUN groupadd -r financetracker && useradd -r -g financetracker financetracker

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py sync_connectors.py ./
COPY templates/ templates/

RUN mkdir -p /data && chown -R financetracker:financetracker /data /app
USER financetracker

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "app:app"]
