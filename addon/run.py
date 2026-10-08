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
    "ha_url": "",
    "ha_token": "",
    "ha_instance": "home",
    "ha_namespace": "home",
    "sync_interval": 300,
}.items():
    os.environ["HA_RECALL_" + key.upper()] = str(options.get(key, default))
identities_path = Path("/data/identities.json")
identities_path.write_text(json.dumps(options.get("identities", [])), encoding="utf-8")
identities_path.chmod(0o600)
os.environ["HA_RECALL_IDENTITIES_FILE"] = str(identities_path)
os.environ.update(HA_RECALL_HOST="0.0.0.0", HA_RECALL_DB="/data/memory.sqlite3")

from ha_recall.server import main  # noqa: E402

main()
