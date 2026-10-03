# LIFELOG web app. Secrets are NOT baked in: pass them at run time
#   docker run --env-file .env -p 8000:8000 lifelog
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lifelog ./lifelog
COPY sql ./sql
COPY static ./static
COPY scripts ./scripts
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s \
  CMD python -c "import json,urllib.request,sys; sys.exit(0 if json.load(urllib.request.urlopen('http://localhost:8000/api/health'))['database'] else 1)"
# one worker: the SSE broker, LISTEN thread and simulator are per process
CMD ["sh", "-c", "uvicorn lifelog.app:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
