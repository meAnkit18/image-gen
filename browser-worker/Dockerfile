FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app
COPY requirements-worker.txt ./requirements-worker.txt
RUN pip install --no-cache-dir -r requirements-worker.txt
COPY worker.py ./worker.py
EXPOSE 8080
CMD ["python", "worker.py"]
