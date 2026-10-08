# HA re:call 1.0

Lokální strukturovaná paměť pro Home Assistant a Jarvis. Jeden proces FastMCP,
SQLite/WAL/FTS5, entity, atomická fakta, vztahy, historie a hybridní hledání.
Bez webového frontendu, PostgreSQL, SSO a samostatného workeru.

Samostatný repozitář vychází z [panuhen/recall](https://github.com/panuhen/recall),
MIT. Zachovává původní Git historii; `upstream-base` označuje výchozí commit
`4cb572e`. GitHub nepovoluje dvojtečku v názvu, proto repozitář `HA_re-call`
a produkt **HA re:call**. Původní zdroje jsou nezměněné v `upstream/` a do runtime
se neinstalují.

## Funkce a kompatibilita

Všech **40 původních MCP nástrojů včetně ping** zůstává aktivních, se stejnými
názvy, argumenty a výchozími hodnotami. K nim přibylo 15 entitových nástrojů;
server tedy nabízí **55 nástrojů**. Původní rozhraní nebylo zredukováno.

- Poznámka je Markdown popis entity ve stejné databázi. Zápisy přes `create_note`
  i `create_entity` jsou navzájem viditelné. Wiki odkazy tvoří grafové vztahy.
- Fungují složky, přesuny, kopie, verze, obnovení, koš, kontrola odkazů, tagy,
  konvence a termíny kontroly poznámek. Přesun do jiné kolekce zachovává historii.
- Workspace je kolekce. Původní sdílecí nástroje spravují lokální tokenové identity
  a role owner/editor/viewer. Pozvánka čeká na lokální identitu se shodným UPN;
  neposílá email a nepotřebuje týmovou službu. `org_access=viewer` znamená všechny
  nakonfigurované tokenové identity této instance.
- Webové URL v odpovědích jsou `null`. `preview_diagram` kontroluje strukturu
  Mermaid a vrací text; bez frontendu nevykresluje obrázek.
- Stabilní HA registry ID, aliasy, kategorie, fakta se zdrojem a časovou platností,
  vztahy s atributy. Živé stavy a ovládání zařízení zůstávají v HA.
- Přesné ID/aliasy, český fulltext bez diakritiky a Model2Vec. `entity_context`
  rozvine aktuální fakta a sousedy nalezené entity.
- Změny mají revizi, UTC čas, autora a historii. `expected_revision` chrání nové
  rozhraní před souběžným přepsáním; původní `update_note` má `base_updated_at`.
- Mazání je obnovitelné. Pouze výslovný `purge` trvale odstraní obsah i jeho verze.

Konflikty se určují podle entity a predikátu v překrývajícím se časovém intervalu.
Nová hodnota je `pending`, dokud ji `resolve_fact` nepřijme nebo nezamítne.
Nezávislá pozorování používají `exclusive=false`. Rozpor formulovaný jinými
predikáty musí rozpoznat volající LLM. Přijetí návrhu nahradí celý překrývající se
fakt, nerozděluje jeho interval. `list_records` je administrativní pohled včetně
faktů mimo aktuální platnost; hledání a kontext platnost respektují.

## Spuštění zde na počítači

Python 3.12+ a uv:

```powershell
uv sync --locked
./tools/run-local.ps1
```

Skript vytvoří náhodný token v ignorovaném `data/mcp.token`. Server běží na
`127.0.0.1:8004/mcp`; klient posílá `Authorization: Bearer <token>`.
`/health` vrací dostupnost databáze a stav synchronizace bez obsahu paměti.
`Ctrl+C` server zastaví; data zůstávají v `data/memory.sqlite3`.

Pro existující lokální Model2Vec:

```powershell
uv sync --locked --extra local-model
$env:HA_RECALL_MODEL_PATH = 'C:/cesta/k/existujicimu/modelu'
./tools/run-local.ps1
```

Adresář obsahuje `config.json`, `tokenizer.json`, `model.safetensors`. Váhy se
nestahují. Alternativou je `HA_RECALL_EMBEDDING_URL` s úplnou adresou
OpenAI-kompatibilního embeddingového endpointu a `HA_RECALL_EMBEDDING_KEY`.
Lokální model a HTTP endpoint nelze zapnout současně.

Bez embeddingů je hledání přesné/fulltextové (`semantic: disabled`); při výpadku
endpointu se zachová fulltext a vrátí `unavailable`. Změna modelové identity
zneplatní cache. Při dotazu se dopočítá nejvýše 32 chybějících záznamů;
`reindex_memory` umožňuje větší import zpracovat po dávkách. Vektory jsou v SQLite
s lineárním výpočtem podobnosti pro domácí kolekce.

## Lokální identity a soukromí

Hlavní `HA_RECALL_TOKEN` má alespoň 32 znaků a identitu `local`. Výchozí kolekce
`home,technical` vlastní tato identita. `HA_RECALL_NAMESPACES=home,technical,private`
přidá další osobní kolekci. Další tokeny nemají k těmto kolekcím přístup, dokud jim
jej vlastník nepřidělí pomocí `share_workspace`.

Ve standalone režimu nastavte `HA_RECALL_IDENTITIES_FILE` na neveřejný JSON:

```json
[{"id":"jarvis","upn":"jarvis@local","token":"SEM_PATRI_NAHODNY_TOKEN_ALESPON_32_ZNAKU"}]
```

ID i UPN jsou stabilní; měňte token, ne identitu. Po úpravě souboru restartujte
server. V doplňku totéž nastavuje pole `identities`. Sdílený token nerozlišuje
jednotlivé lidské mluvčí. Jarvis používá oprávnění tokenu nastaveného v integraci;
pro hlasový přístup sdílejte jen zamýšlené kolekce. Text se posílá pouze na
embeddingový endpoint, který sami nakonfigurujete.

## Instalace připravených balíčků do HA

Tato instalace není automatickou součástí vývoje. Vyžaduje **HA Core 2026.10.0
nebo kompatibilní novější verzi**, HA OS/Supervised pro doplněk a existující
`jarvis_semantic` pro sdílení jeho Model2Vec. Bez Jarvis modelu lze používat
fulltext nebo jiný embeddingový endpoint.

Balíčky vytvoří:

```sh
uv run --locked python tools/package_addon.py
```

1. Rozbalte `dist/ha_recall-addon.zip` do `/addons/`, aby vzniklo
   `/addons/ha_recall/config.json`. Obnovte seznam lokálních doplňků a sestavte
   HA re:call. Nastavte náhodný `token`, transport `http` a doplněk spusťte.
2. Rozbalte `dist/ha_recall-integration.zip` do HA `/config/`, aby vzniklo
   `/config/custom_components/ha_recall/manifest.json`. Restartujte HA Core.
3. V Nastavení → Zařízení a služby → Přidat integraci vyberte **HA re:call**.
   Zadejte interní adresu doplňku `http://local-ha-recall:8004/mcp` a stejný token.
   Pokud instalace používá jiný hostname, použijte hostname z informací doplňku.
   Pro transport `sse` zadejte `/sse`.
4. Zapněte přístup pro Assist/Jarvis podle zamýšlených oprávnění. Integrace
   přidá všech 55 nástrojů s prefixem `ha_recall_` do Assist API a nabízí také
   samostatné LLM API **HA re:call**. Jarvis/Luna používající Assist je tak uvidí
   bez změny svého zdrojového kódu. Výchozí přístup pro Assist je vypnutý.
5. Pro sdílení již načteného Model2Vec ponechte zapnutý embeddingový most.
   V doplňku nastavte `embedding_url` na
   `http://<adresa-HA>:8123/api/ha_recall/embeddings` a `embedding_key` na
   dlouhodobý HA token. Jde o **HA token**, nikoli MCP token z kroku 1.
6. Pro automatické registry nastavte `ha_url` na `http://<adresa-HA>:8123`,
   `ha_token` na HA token uživatele oprávněného číst registry, `ha_instance`
   na stabilní označení domácnosti, `ha_namespace` na `home` a `sync_interval`
   např. 300 sekund. Po změně konfigurace restartujte doplněk.

Most volá přímo již načtený `DecisionLayer.model.encode`, bez intentového
předzpracování a bez druhé kopie vah. Endpoint vyžaduje HA autentizaci a omezuje
velikost i souběh požadavků. Při chybě modelu vrací 503; paměť dál poskytuje
fulltext. Připojení k MCP využívá HTTP/SSE klienta přímo z HA Core, s bearer tokenem.
Není potřeba OAuth ani vypínání autentizace.

Doplněk nemá webový ingress ani přístup k Supervisor API. Data jsou v `/data`
a patří do zálohy doplňku. Ve výchozím stavu nepublikuje port mimo interní síť HA.
Při vývoji se žádná integrace ani doplněk do běžícího domácího HA nekopíruje.

## HA registry a stabilní identita

Periodická synchronizace běží ve stejném procesu. Používá jen WebSocket příkazy
`config/area_registry/list`, `config/device_registry/list`,
`config/entity_registry/list`, nikdy `get_states` ani volání služeb.
`/health.registry_sync` ukazuje `ready`, `unavailable`, `pending` nebo `disabled`
a čas posledního úspěchu. Chyba nezastaví paměť; další interval zopakuje pokus.

Registry UUID přežijí přejmenování `entity_id`; aktuální i původní HA ID zůstávají
aliasy. Ruční popisy a kategorie se zachovají. Import opraví vlastní vazby na
místnosti, ruční vztahy nemění. Uživatelův koš neobnovuje. Reference chybějící
v pozdějším snapshotu zachová, protože nepřítomnost nemusí znamenat odstranění.
Snapshot se zapíše atomicky. Token HA není součástí paměťové databáze.

Jednorázový import nebo offline snapshot:

```powershell
$env:HA_RECALL_HA_TOKEN = '<HA token>'
uv run --locked ha-recall-import --instance byt --ha-url http://homeassistant.local:8123
uv run --locked ha-recall-import --instance byt --snapshot registries.json
```

Snapshot má pole `areas`, `devices`, `entities` ve formátu HA registry API.
`--instance` neměňte mezi importy téže domácnosti.

## Samostatný kontejner, záloha a aktualizace

```sh
cp .env.example .env
# Vyplňte náhodný HA_RECALL_TOKEN a případné lokální endpointy.
docker compose up --build -d
```

Compose ve výchozím stavu publikuje port pouze na loopback; pro jiné zařízení
nastavte vlastní bind adresu. Přes nedůvěryhodnou síť použijte TLS proxy.
Kontejner běží jako UID 10001, s kořenovým filesystemem jen pro čtení a datovým
volume `memory`. Volitelný build `WITH_LOCAL_MODEL=1` přidá knihovnu Model2Vec;
váhy připojte samostatně read-only. Prostředí `.env` načítá Compose nebo
`uv run --env-file .env ha-recall`, samotný Python modul nikoli.

```sh
uv run --locked python tools/backup.py --database data/memory.sqlite3 --output backup.sqlite3
```

SQLite backup API zahrne potvrzené zápisy z WAL. Při obnově zastavte server
a obnovte DB do prázdného datového adresáře. Nekopírujte pouze hlavní soubor DB
během běhu. Záloha zahrnuje poznámky, entity, historii, koš i lokální oprávnění.
Konfigurační tokeny zálohujte odděleně. `export_memory` je čitelný export záznamů
a historie jedné kolekce, nikoli náhrada úplné DB zálohy.

Databáze lokální verze 0.1 se otevírá přímo, bez ztráty záznamů; nové tabulky
kompatibility se vytvoří automaticky. Před aktualizací vždy vytvořte zálohu.
Import původní vzdálené PostgreSQL databáze není automatický ani potřebný pro
novou prázdnou domácí instalaci.

## Ověření

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check ha_recall tests_ha tools addon/run.py custom_components tests_ha_runtime
uv run --locked python tools/package_addon.py
docker build -t ha-recall:local .
uv run --locked python tools/docker_smoke.py --image ha-recall:local
docker build -f tests_ha_runtime/Dockerfile -t ha-recall-ha-test:local .
uv run --locked python tools/ha_runtime_check.py --model /cesta/k/existujicimu/modelu
uv run --locked python tools/ha_runtime_check.py --model /cesta/k/existujicimu/modelu --transport sse
```

Test posledních dvou příkazů spustí dočasný HA Core 2026.10.0 a backend ve vlastní
Docker síti. Ověřuje konfigurační průvodce, nesprávné tokeny, skutečné Assist API,
55 nástrojů, české hledání přes sdílený Model2Vec, registry a odpojení integrace.
Po testu odstraní své kontejnery, volume, síť a testovací token. Domácí HA
nepoužívá. Volba `--fixture-model` v CI nahrazuje pouze váhy deterministickým
modelem; ověřuje integraci a není testem sémantické kvality.

Regresní testy dále pokrývají konflikt faktů, časovou platnost, oddělení kolekcí,
původní signatury, poznámky a entity ve stejném grafu, historie, koš, přesuny,
modelovou cache, souběžné úpravy a zálohy. Kontejnerové testy ověřují oba
transporty, přístupová práva a zachování dat po restartu.
