FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements-ha.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-ha.txt
COPY ha_recall ./ha_recall
COPY run.py ./run.py
EXPOSE 8004
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8004/health', timeout=4)"
CMD ["python", "run.py"]
