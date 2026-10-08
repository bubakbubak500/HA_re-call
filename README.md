# HA re:call

Lokální strukturovaná paměť pro Home Assistant, odvozená z
[panuhen/recall](https://github.com/panuhen/recall), MIT.
Samostatný repozitář zachovává původní historii; značka `upstream-base` označuje
výchozí commit `4cb572e`. GitHub nepovoluje dvojtečku v názvu repozitáře,
proto `HA_re-call`, zatímco název produktu je **HA re:call**.

## Co funguje ve verzi 0.1

- Jeden Python proces, FastMCP, SQLite/WAL/FTS5. Bez PostgreSQL, webového frontendu,
  SSO, týmových rolí a samostatného workeru v novém runtime.
- Entity, stabilní externí identifikátory, aliasy, kategorie a volitelný Markdown popis.
- Atomická fakta se zdrojem a intervalem platnosti; vztahy se zdrojem a atributy.
- Konzervativní detekce konfliktů stejného predikátu v překrývajícím se intervalu.
  Nový návrh je `pending`, dokud jej asistent výslovně nepřijme nebo nezamítne.
- Historie každého zápisu a kontrola `expected_revision` proti souběžnému přepsání.
- Obnovitelné mazání; smazání entity skryje i její fakta a vazby.
- Hledání podle přesného ID/aliasu, českého fulltextu bez diakritiky a volitelných
  embeddingů. `entity_context` rozvine aktuální fakta, vztahy a sousední entity.
- Model2Vec buď přes OpenAI-kompatibilní HTTP endpoint, nebo přímo z existujících
  lokálních vah. Žádné automatické stahování modelu ani volání LLM.
- Bearer token, Streamable HTTP `/mcp` nebo SSE `/sse` (volba při spuštění).
- Atomický a opakovatelný import HA registrů, bez živých stavů nebo ovládání HA.

### Původní MCP nástroje

Původní re:call je **beze změn archivovaný v `upstream/`**, včetně všech nástrojů
v `upstream/src/tools/`. V této etapě nejsou přepisovány ani slučovány do šesti
navržených nástrojů. Nový SQLite server má vlastní entitové nástroje a původní
Markdown/týmové nástroje **nenačítá**. Není tedy náhradou původního serveru se
stejným API. Jejich případný převod na nový datový model zůstává samostatnou etapou.
Archivovaný web a týmový backend se neinstalují do nového balíčku ani kontejneru.

## Lokální spuštění

Python 3.12+ a [uv](https://docs.astral.sh/uv/):

```powershell
uv sync --locked
# Vytvoří lokální token v ignorovaném data/mcp.token a spustí server:
./tools/run-local.ps1
```

Server naslouchá na `127.0.0.1:8004`, MCP endpoint je `/mcp` a veřejný healthcheck
`/health` nevrací obsah paměti. Klient musí posílat
`Authorization: Bearer <obsah data/mcp.token>`. Token nikdy nevkládejte do URL.
`Ctrl+C` ukončí server; SQLite databáze zůstává v `data/memory.sqlite3`.

Ruční spuštění (PowerShell):

```powershell
$env:HA_RECALL_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
uv run --locked ha-recall
```

Na Linuxu odpovídá `export HA_RECALL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"`.
`.env` se načítá pomocí `uv run --env-file .env ha-recall` nebo automaticky v Docker Compose.
Samotný modul Pythonu `.env` nenačítá.

## Model2Vec a vyhledávání

Bez konfigurace jsou dostupné přesné a fulltextové dotazy; odpověď uvádí
`semantic: disabled`. Při poruše embeddingového serveru vrátí `unavailable` a
zachová fulltextové výsledky. Dostupnost se netají za falešnou sémantikou.

Pro existující HTTP službu nastavte úplnou URL endpointu:

```powershell
$env:HA_RECALL_EMBEDDING_URL = 'http://127.0.0.1:8090/v1/embeddings'
$env:HA_RECALL_EMBEDDING_MODEL = 'model2vec'
```

Požadavek: `{"model":"model2vec","input":["text"]}`.
Odpověď: `{"data":[{"index":0,"embedding":[0.1,0.2]}]}`.
Endpoint přijímá texty pouze z právě použité kolekce. Zadáním externí URL
odesíláte texty této službě; pro domácí paměť používejte vlastní lokální službu.

Pro přímé načtení **již staženého** modelu:

```powershell
uv sync --locked --extra local-model
$env:HA_RECALL_MODEL_PATH = 'C:/cesta/k/modelu'
./tools/run-local.ps1
```

Adresář musí obsahovat `config.json`, `tokenizer.json` a `model.safetensors`.
Použití HTTP a lokálního modelu současně je odmítnuto. Změna modelu zneplatní
cache. Embeddingy se dopočítávají v procesu při hledání, nejvýše 32 záznamů na
dotaz; nástroj `reindex_memory` zpracuje větší import po dávkách. Vektory jsou
v SQLite a podobnost se pro malé domácí kolekce počítá lineárně v paměti.

V tomto HA projektu Model2Vec běží uvnitř `jarvis_semantic`; samostatný HTTP
embeddingový endpoint není potvrzený. Jeho zpřístupnění musí předcházet přímému
napojení z doplňku. Lokální varianta je ověřitelná skriptem:

```powershell
uv run --extra local-model python tools/local_model_check.py 'C:/cesta/k/modelu'
```

## Příklad práce s pamětí

1. `create_entity(namespace="home", entity={"name":"EGLO žárovka", "entity_type":"device", "external_id":"light.eglo_living_room"})`.
2. Vytvořte oblast `Obývák` a propojte její vrácené UUID přes `add_relation`
   s `predicate="located_in"`, `subject_id`, `object_id`, `source="user"`.
3. `add_fact` uloží např. `predicate="pairing_issue"`, `value="Vyřešeno resetem"`,
   `entity_id` a `source="user"`. Nezávislá pozorování mají `exclusive=false`.
4. Jiná hodnota stejné vlastnosti vytvoří návrh. `resolve_fact` s `accept=true`
   a aktuální revizí nahradí překrývající se přijaté hodnoty; staré zůstanou v historii.
5. `search_memory` najde entitu či fakt. `entity_context` nad oblastí ukáže
   zařízení v místnosti; nad zařízením jeho aktuální fakta.

Další nástroje: `ping`, `list_namespaces`, `update_entity`, `get_record`,
`list_records`, `list_history`, `forget_record`, `restore_record`,
`reindex_memory`, `export_memory`.

Konflikty jsou určovány podle entity a predikátu, ne jazykovým modelem.
Rozpory mezi různými názvy predikátů musí rozpoznat volající asistent.
Přijetí návrhu zneplatní celý překrývající se starý fakt; nerozděluje jej na časové
úseky. Platnost se uplatňuje v hledání a kontextu. `list_records` je administrativní
pohled a zahrnuje i přijaté fakty mimo aktuální interval platnosti.

## Kolekce, token a historie

Výchozí kolekce jsou `home,technical`. `private` zpřístupněte jen explicitním
nastavením `HA_RECALL_NAMESPACES`. Jeden serverový token má přístup ke všem
vyjmenovaným kolekcím; nejde o víceuživatelské ACL. Pro oddělenou soukromou
paměť použijte samostatnou instanci, databázi a token. Vztahy nesmí přecházet mezi
kolekcemi. Server bez tokenu o délce alespoň 32 znaků odmítne nastartovat.

Každá změna má čas UTC, akci, revizi a autora (`local` pro sdílený token,
`ha-registry:<instance>` pro import). Historie neidentifikuje jednotlivé osoby
používající tentýž token. `restore_record` vytváří novou revizi, historii nemaže.
Obnova entity automaticky neobnovuje její smazané potomky; obnovte je jednotlivě.
Obnova konfliktního faktu znovu vyžaduje přijetí návrhu.

## Import referencí z HA

Import je explicitní administrátorský příkaz, ne nástroj dostupný LLM:

```powershell
# Token HA předat prostředím, nikdy argumentem v příkazové řádce.
$env:HA_RECALL_HA_TOKEN = '<HA long-lived token>'
uv run --locked ha-recall-import --instance byt --ha-url http://homeassistant.local:8123
# Alternativa: dříve získaný snapshot tří registrů:
uv run --locked ha-recall-import --instance byt --snapshot registries.json
```

Snapshot má pole `areas`, `devices`, `entities` ve formátu HA registry API.
`--instance` musí být stabilní. Registry UUID přežijí přejmenování `entity_id`;
aktuální i původní HA ID jsou aliasy. Ruční popisy a kategorie zůstávají zachované.
Přesun zařízení opraví importované vazby, ruční vztahy zachová. Znovu se neobnoví
záznamy, které uživatel smazal. Reference chybějící v novém snapshotu zůstávají
v paměti; import nepředpokládá, že nepřítomnost znamená trvalé odstranění.
Všechny změny snapshotu se zapisují v jedné transakci.

Síťový klient používá jen `config/area_registry/list`, `config/device_registry/list`
a `config/entity_registry/list`. Nepoužívá `get_states`, odběr událostí ani volání
služeb. HA token se do databáze neukládá.

## Jeden kontejner

```sh
cp .env.example .env
# Vyplňte náhodný HA_RECALL_TOKEN.
docker compose up --build -d
```

Výchozí port je publikovaný jen na loopback. Pro přístup ze sítě nastavte vlastní
Compose override s konkrétní bind adresou; pro komunikaci přes nedůvěryhodnou síť
přidejte TLS proxy. Data jsou v pojmenovaném volume `memory`. Kontejner běží pod
UID 10001 a kořenový filesystem je pouze pro čtení. Modelové váhy nejsou v image.
Volitelný build `--build-arg WITH_LOCAL_MODEL=1` přidá Model2Vec knihovnu; model
pak připojte read-only a nastavte `HA_RECALL_MODEL_PATH`.

## Budoucí instalace do Home Assistantu

```sh
uv run --locked python tools/package_addon.py
```

Výstup `dist/ha_recall-addon.zip` obsahuje lokální doplněk `ha_recall/`.
Rozbalte jej do `/addons/`, obnovte seznam lokálních doplňků a sestavte HA re:call.
V konfiguraci nastavte token. Doplněk má ruční spouštění, nemá ingress, přístup
k Supervisor API ani implicitně publikovaný hostitelský port. Databáze je v `/data`
a patří do zálohy doplňku. Embeddingy doplněk odebírá z volitelné HTTP služby.

**SSE transport s bearer tokenem není automaticky OAuth integrace pro HA.**
[Standardní HA MCP klient](https://www.home-assistant.io/integrations/mcp/)
popisuje SSE a OAuth Client ID/Secret. Tato verze poskytuje lokální bearer token,
nikoli OAuth server. Použijte klienta, který umí Authorization header (např.
vlastní Jarvis MCP klient), nebo později doplňte autentizační adaptér. Neobcházejte
to vypnutím autentizace nebo vložením tokenu do URL.
Provoz v HA ani přímé připojení jeho standardní MCP integrace zatím není ověřené.

## Testy, záloha a stav migrace

```sh
uv sync --locked
uv run --locked pytest
uv run --locked ruff check ha_recall tests_ha tools addon/run.py
uv run --locked python tools/backup.py --database data/memory.sqlite3 --output backup.sqlite3
```

Zálohování používá SQLite backup API a zahrne i potvrzené zápisy ve WAL.
Pro obnovu zastavte server a obnovte databázi ze zálohy do prázdného datového
adresáře. Nekopírujte jen hlavní `.sqlite3` soubor během běhu; může chybět WAL.
`export_memory` poskytuje čitelný JSON včetně historie; není náhradou DB restore.

CI kontroluje lint, testy, oba transporty, balení lokálního doplňku a sestavení
obou Docker image. Reálný Model2Vec test je volitelný a model v CI nestahuje.

Připravený je samostatný lokální základ pro novou paměť. Před produkčním HA
nasazením zbývá ověřit add-on na cílovém zařízení, autentizaci jeho MCP klienta
a přístup k existujícím Model2Vec embeddingům. Automatická migrace dat z původního
PostgreSQL a převod původních MCP nástrojů součástí této etapy nejsou.
