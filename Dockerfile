# ThreadBNC: web UI + embedded bouncer in one container.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    THREADBNC_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY threadbnc ./threadbnc

# Run unprivileged; /data holds archived media and the generated session secret.
RUN useradd --system --uid 10001 --no-create-home --home-dir /app threadbnc \
    && mkdir -p /data && chown threadbnc:threadbnc /data
USER threadbnc
VOLUME ["/data"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "threadbnc", "serve", "--host", "0.0.0.0", "--port", "8080"]
