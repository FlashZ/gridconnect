ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="GridConnect" \
      org.opencontainers.image.description="Local monitoring and control for Tuya smart plugs" \
      org.opencontainers.image.source="https://github.com/FlashZ/gridconnect" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN id -u gridconnect >/dev/null 2>&1 || useradd --create-home --uid 10001 gridconnect; \
    mkdir -p /data && chown gridconnect:gridconnect /data
USER gridconnect
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=5).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
