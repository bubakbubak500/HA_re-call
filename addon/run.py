"""Map Supervisor options to the same fail-closed standalone server."""

import json
import os
from pathlib import Path

options = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
for key, default in {
    "token": "",
    "transport": "sse",
    "namespaces": "home,technical",
    "embedding_url": "",
    "embedding_model": "model2vec",
    "embedding_key": "",
}.items():
    os.environ["HA_RECALL_" + key.upper()] = options.get(key, default)
os.environ.update(HA_RECALL_HOST="0.0.0.0", HA_RECALL_DB="/data/memory.sqlite3")

from ha_recall.server import main  # noqa: E402

main()
