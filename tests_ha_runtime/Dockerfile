ARG HA_VERSION=2026.10.0
FROM ghcr.io/home-assistant/home-assistant:${HA_VERSION}
# Test-only image. Production integration reuses Jarvis's loaded Model2Vec.
RUN uv pip install --system model2vec==0.9.0 tokenizers==0.22.2
ENV PYTHONPATH=/work PYTHONUNBUFFERED=1
WORKDIR /work
ENTRYPOINT ["python", "tests_ha_runtime/runner.py"]
