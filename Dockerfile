# Slim image: the service is pure HTTP, so no browser runtime is needed.
# That is what keeps it inside Render's free 512 MB / 0.1 CPU instance.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# Dependencies first so code edits do not invalidate the wheel cache layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080

# Single worker on purpose: the key pool and rate limiter are per-process.
# Render allows very long responses (up to 100 minutes), so a synchronous
# generation request is safe.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 120"]
