"""Optional real-model Czech retrieval check; synthetic data only."""

import argparse
import asyncio
import json

from ha_recall.models import Entity, Fact
from ha_recall.search import LocalEmbeddings, Search
from ha_recall.store import Store


async def run(path):
    store = Store(":memory:")
    try:
        provider = LocalEmbeddings(path)
        search = Search(store, provider)
        bulb = store.create_entity(
            "home", Entity(name="EGLO žárovka v obýváku", entity_type="device")
        )
        store.add_fact(
            "home",
            Fact(
                entity_id=bulb["id"],
                predicate="problém",
                value="Nedařilo se párování osvětlení, pomohl reset žárovky.",
                source="synthetic-demo",
            ),
        )
        store.create_entity("home", Entity(name="Teploměr v zahradě", entity_type="device"))
        store.create_entity("home", Entity(name="Pračka v koupelně", entity_type="device"))
        result = await search.search("home", "potíže s připojením lampy", limit=2)
        assert result["semantic"] == "ready"
        assert any(
            r["kind"] == "fact" and r["data"]["entity_id"] == bulb["id"] for r in result["results"]
        )
        print(
            json.dumps(
                {
                    "semantic": result["semantic"],
                    "matches": [
                        {"kind": r["kind"], "text": store.text(r), "matches": r["matches"]}
                        for r in result["results"]
                    ],
                },
                ensure_ascii=False,
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_path")
    asyncio.run(run(parser.parse_args().model_path))
