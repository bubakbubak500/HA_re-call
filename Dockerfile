FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HA_RECALL_HOST=0.0.0.0 HA_RECALL_PORT=8004 HA_RECALL_DB=/data/memory.sqlite3
COPY requirements-ha.txt requirements-local-model.txt ./
ARG WITH_LOCAL_MODEL=0
RUN pip install --no-cache-dir --require-hashes -r requirements-ha.txt \
    && if [ "$WITH_LOCAL_MODEL" = "1" ]; then pip install --no-cache-dir --require-hashes -r requirements-local-model.txt; fi \
    && useradd --uid 10001 --create-home recall \
    && mkdir /data && chown recall:recall /data
COPY ha_recall ./ha_recall
USER 10001:10001
EXPOSE 8004
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8004/health', timeout=4)"
CMD ["python", "-m", "ha_recall.server"]
