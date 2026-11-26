# DESIGN.md — MIND v0 (slice 0)

Контракт схемы, CLI и моста к mem. Источник истины для кода — этот файл.
Код: `mind.py` + `mem-bridge.py`. Python 3.10+, только stdlib, 0 внешних зависимостей.

## 0. Принципы

1. md = источник истины; `mind.db` — производный индекс (auto-resync при дрейфе файлов).
2. Версии / tombstone / связи — семантика как в mem (`bump_version`, `supersedes`, `parent_id`, `related`).
3. Связь с mem однонаправленная: только чтение через MCP (`mem-bridge.py`), цитаты `id@vN`.
4. Никаких правок в `F:\AI\MEM` и `F:\AI\OLAMA\memory`.
5. ASCII в коде; русский — в md/документации.
6. Embedding в v0 НЕТ: recall честно печатает `mode: fts (vectors: n/a)`.

## 1. Раскладка

```
F:\AI\MIND\
├── mind.py              # CLI + ядро
├── mem-bridge.py        # MemBridge: MCP-клиент к mem (единственная точка касания)
├── DESIGN.md            # этот файл
├── CONTRACT.json        # пиннинг ожидаемого контракта mem
├── MIND.md              # индекс-оглавление (генерируется sync, как MEMORY.md)
├── MIND\                # md-источник истины
│   ├── trait\  habit\  idea\  insight\  setting\
├── mind.db              # SQLite (генерируется)
├── logs\                # doctor/digest логи
├── docs\calibration.md  # калибровочный лист черт
└── tests\               # unittest, MIND_ROOT -> tempdir
```

Корень переопределяется env `MIND_ROOT` (аналог `MEM_ROOT`) — обязательно для тестов.

## 2. Frontmatter-контракт

Обязательные (пишет sync, если отсутствуют/битые): `id, type, path, status, version, author, updated`.
Опциональные: `importance` (1–5, дефолт 3), `confidence` (0.0–1.0, дефолт 0.5),
`related_mind` (id через запятую), `related_mem` (цитаты `N@vN` через запятую),
`supersedes` (путь предыдущей версии), `session`.

`id` вручную не пишется: sync присваивает (max+1) молча при первом индексировании.
Файлы без id допустимы — это штатный путь для импорта.

### Тип-специфичные поля (валидируются sync и remember)

| type | обязательные extra-поля |
|---|---|
| `trait` | `scope, value, direction, falsified_when` |
| `habit` | `scope, trigger, action` |
| `idea` | `hypothesis`; при status>=testing — `criterion`; при status=lost — `killed_by` |
| `insight` | `summary, prediction` |
| `setting` | `mode` (одно из `off, hint, block`) |

`scope` обязателен для всех типов, кроме `setting`: `agent | project | companion`.
У `setting` scope опционален и дополнительно допустим `all`.
`value` у trait: число 0.0–1.0.
`related_mem`: каждая цитата матчится `^\d+@v\d+$` (иначе warn).

Опциональные тип-поля (примеры): у trait — `evidence_mem, evidence_mind, counter_evidence`;
у habit — `eligibles, hits, exceptions, approved_by, approved_at, expires`;
у idea — `expected, criterion, cost, preconditions, resurrected, killed_by, killed_at`;
у insight — `falsifiable_by, reflected_on`; у setting — `block_conditions, override`.

## 3. Матрица статусов

| type | допустимые status |
|---|---|
| trait | candidate, active, superseded, deleted |
| habit | candidate, active, deprecated, superseded, deleted |
| idea | spark, drafted, testing, won, lost, parked, superseded, deleted |
| insight | active, superseded, deleted |
| setting | active, superseded, deleted |

`alive` = status не в {superseded, deleted}. recall/list по умолчанию — только alive
(`--status all` показывает всё; это осознанное отличие от mem, где list без фильтра шумит).

## 4. Идентификаторы, версии, tombstone

- `update` (содержательное изменение): новый файл `<path>-vN.md` с НОВЫМ `id`,
  старый файл -> `superseded`, новый получает `supersedes: <старый путь>`;
  sync строит `parent_id` (как mem, второй проход :701–722).
- Цитата: `id@vN` (vN = version этой записи).
- `forget` -> `deleted` (tombstone), `predelete_status` запоминается; `restore` возвращает
  именно этот статус и отказывает только если воскрешение сделает запись живой при
  живом ребёнке (restore в `superseded` ребёнка не касается).
- **Операционные поля не бампят версию** (правка in-place, как `link` в mem):
  `exceptions, approved_by, approved_at, eligibles, hits, resurrected, expires,
  predelete_status, killed_by, killed_at, approved/status-переходы lifecycle`.
  Содержательные поля (`direction, hypothesis, summary, action, mode, ...`) — только через `update`.
- Lifecycle-команды (`idea status`, `habit approve`, ...) правят frontmatter in-place
  (status + тип-поля), версию не бампят: история lifecycle = эти поля + `updated`.

## 5. CLI

```
python mind.py sync [--lenient]
python mind.py remember (--type T [--path P] [--scope S] [--importance N]
                         [--related-mem "a,b"] [--related-mind "i,j"]) (TEXT | --file F)
python mind.py update ID (TEXT | --file F)
python mind.py forget ID
python mind.py restore ID
python mind.py list [--type T] [--status S|all] [--json]
python mind.py show ID
python mind.py link ID [IDS...] [--off IDS...]
python mind.py recall QUERY [--limit N] [--type T] [--json]

python mind.py idea new --path P (--hypothesis H | --file F)
                         [--expected E] [--criterion C] [--cost X] [--related-mem ...]
python mind.py idea status ID STATUS      # гейты/переходы, см. §6
python mind.py idea kill ID --by REASON   # lost + killed_by

python mind.py habit approve ID           # candidate -> active, approved_by=user, approved_at=today
python mind.py habit exception ID --why TEXT
python mind.py habit revise ID --keep|--deprecate

python mind.py profile [--scope S] [--json]
python mind.py digest [--json]
python mind.py doctor mem [--json] [--deep]
```

Режимы вне v0 (НЕ реализуются, не регистрируются): `serve, reflect, resurrect, embed`.

Коды возврата: 0 — ок; 1 — drift/валидация/ничего не найдено; 2 — ошибка/недоступен mem (doctor).

`--file` может содержать frontmatter целиком: заданные значения побеждают,
недостающие обязательные берутся из флагов; тип-поля валидируются. Иначе — TEXT-аргумент
(ровно один источник: TEXT или --file, как в mem; rc=2 при обоих/ни одного).
Исключения: `update --file` не принимает `id, version, supersedes, updated, path, type`
(операционная идентичность — из самой записи), а `remember`/`idea new` не могут создать
idea/habit сразу в не-entry статусе (idea: `spark`, habit: `candidate`) — это обход
WIP-лимита и гейта approve.

`remember` без `--path`: path = slug из первого слова/типа (дефолт: `type-YYYYMMDD-HHMMSS`).

## 6. Машина состояний idea (гейты исполняет код)

```
spark   -> drafted | parked
drafted -> testing | parked            # testing ТРЕБУЕТ непустой criterion
testing -> won | lost                  # lost ТРЕБУЕТ killed_by (idea kill)
parked  -> drafted | testing           # те же гейты
won/lost -> терминальные (переоткрытия нет; `lost` ставится только `idea kill`)
```

`update --file` со сменой статуса идеи проходит те же гейты (переходы, criterion, WIP),
что и `idea status`; `lost` через update запрещён — только `idea kill ID --by REASON`.

WIP-лимит: одновременно `drafted+testing` <= 7. При попытке перевести в drafted/testing
при WIP>=7 -> rc=1, сообщение «WIP full, сначала parked/win/lost».

Валидация и в `sync`: файл со status=testing без `criterion` или lost без `killed_by`
-> STRICT (дефолт): пропуск + перечень ошибок; `--lenient`: индексирует с warn.

## 7. Жизненный цикл habit

```
candidate --approve--> active --revise--> deprecated
                |            |
                |            +-- exceptions>=3 ИЛИ expires<=today -> флаг ревизии (digest)
                +-- отклонён = оставить candidate (нет отката-команды в v0)
```

`habit exception`: appends `{date, why}` in-place. Ревизия через `expires` (90 дней дефолт).
Привычка с `status: candidate` НИКОГДА не попадает в profile как действующая.

## 8. Recall (v0 = FTS)

- Источник поиска: `search_text` = тело файла + все скалярные значения frontmatter
  (черты живут в полях, а не в теле — без этого recall по ним слеп).
- FTS5 по `search_text`; bm25 min-max нормировка; формула (как mem FTS-ветка :154):
  `score = 0.60*bm25 + 0.25*recency + 0.15*importance_norm`,
  `recency = 0.5^(age_days/30)`, `importance_norm = clamp(1..5)/5`.
- Фильтр: alive (+ `--type`, сортировка score desc).
- Дефолт: limit 10, бюджет 6000 B на тела (как mem).
- Вывод: `id@vN [type] path (updated, score)` + тело + `related_mind -> ..` / `related_mem -> ..`
  в конце: `mode: fts (vectors: n/a)`.

## 9. profile и digest

`profile` — я-блок (~500 токенов), только alive:
свежие черты (по scope), active-привычки (кандидаты НЕТ), <=3 свежих insight,
счётчики parked/drafted/testing идей, текущий setting veto-policy.

`digest` — pending одним списком (один раз в сутки, не размазано):
1. candidate-привычки/черты, ждущие approve;
2. идеи: WIP-список, drafted с criterion (готовы к testing), parked с истёкшими preconditions — нет;
3. привычки: expires<=today или exceptions>=3;
4. doctor mem: статус контракта (drift -> warn; mem down -> warn, не фатал);
5. битые `related_mem`-цитаты (сверка показом через bridge, только при живом mem).

`--json` у обоих — структурированный вывод.

## 10. mem-bridge и doctor mem

`CONTRACT.json` (пиннинг):

```json
{
  "contract_version": 1,
  "pinned_at": "2026-11-25",
  "mem_py": "F:\\AI\\OLAMA\\tools\\mem.py",
  "mem_root": "F:\\AI\\OLAMA",
  "protocol": "2024-11-05",
  "serverInfo": {"name": "mem", "version": "2.0.0"},
  "tools": ["remember","recall","search","update_memory","forget","restore","list","show","link"],
  "frontmatter_required": ["type","path","status","version","author","updated"],
  "citation_pattern": "^\\d+@v\\d+$"
}
```

API `mem_bridge.MemBridge` (единственная точка касания mem):

```python
class MemBridge:
    def __init__(self, mem_py: str, mem_root: str, timeout: float = 30.0): ...
    def start(self) -> None                 # spawn: python mem.py serve, cwd=mem_root,
                                            # env MEM_ROOT=mem_root (ИНАЧЕ mem примет наш
                                            # .git за свой маркер корня и будет серверить
                                            # не ту память!)
    def call(self, name: str, arguments: dict) -> dict
                                            # tools/call -> {"text", "isError"}
    def handshake(self) -> dict             # initialize -> {serverInfo, protocolVersion,
                                            #               capabilities}
    def list_tools(self) -> list[str]
    def recall(self, query: str, limit: int = 5, no_vector: bool = True) -> list[dict]
        # -> [{"citation": "76@v3", "score": .., "type": .., "path": ..,
        #      "updated": .., "content": .., "related": ..}, ...]
        #    (парсинг текстового ответа MCP; футер "-- n/m hits" отрезается)
    def show(self, citation: str) -> str | None
        # текст записи; None ТОЛЬКО при явном "not found:" от mem,
        # прочие isError (locked, "not in db") -> BridgeError
    def check_contract(self, contract: dict) -> dict
        # -> {"ok": bool, "drifts": [str], "server": {...}, "tools": [...]}
    def close(self) -> None                 # terminate дочернего процесса
    def __enter__/__exit__
```

Протокол: stdio, newline-delimited JSON-RPC 2.0, `initialize` c
`clientInfo: {"name":"mind","version":"0.1"}`, `tools/list`, `tools/call {name, arguments}`.
Точные имена параметров recall — смотреть в `_mcp_tools` mem.py (~:2254), НЕ гадать.

`python mind.py doctor mem`:
1. bridge.start + handshake + tools/list;
2. сверка с CONTRACT.json: serverInfo.version, protocol, состав tools (set-сравнение);
3. `--deep`: recall "test" -> результат парсится, цитаты матчат citation_pattern;
4. вывод: human | `--json`; коды: 0 ок / 1 drift / 2 mem недоступен.

При дрейфе doctor НЕ падает молча — пишет перечень в stdout/digest.

## 11. Деградация

| Ситуация | Поведение |
|---|---|
| mem недоступен | свои записи и recall работают; bridge-часть digest/doctor -> warn `mem bridge: DOWN (<detail>)`, rc!=0 только у doctor |
| битая цитата | doctor/digest помечают, не чинят молча |
| mind.db удалён/устарел | sync пересобирает из md (auto-resync, как ensure_fresh) |

## 12. Тесты

- `python -m unittest discover -s tests` из `F:\AI\MIND`.
- Каждый тест: `MIND_ROOT` -> `tempfile.TemporaryDirectory()`, `MEM` не трогаем.
- Bridge: `tests/stub_mem_server.py` — дочерний процесс, говорящий протоколом (canned responses);
  интеграционный тест против реального mem — skipUnless файл существует.
- Доктрина: тесты doctor в mind.py — skipUnless `mem-bridge.py` существует.

## 13. Вне v0 (дорожная карта)

slice 1: MCP `serve` mind + тул `profile` в opencode.jsonc.
slice 1.5: embed — гибридный recall (решение пользователя 25.11: «вектора должны быть»).
  - Каскад ТОЛЬКО remote (телефон 5131 через `adb forward tcp:5031 tcp:5131`) → FTS,
    как в mem; локальный embed-server НЕ поднимать (правило AGENTS.md). Телефон
    недоступен/таймаут → молча FTS, футер `mode: fts (remote server down)`.
  - Своя таблица `mind_vec` в mind.db (dim 1024, Q8_0) — в mem_vec НЕ лезть,
    однонаправленность mind→mem сохраняется.
  - Команды: `mind.py embed` (добивка недоставленных векторов) + гибридный
    `recall` с весами, старт с mem-замеренных 0.45/0.20/0.10/0.25
    (bm25/recency/importance/cosine), FTS-веса v0 остаются fallback'ом.
  - Sweep: отдельная задача (hidden vbs) либо шаг в Mem-Sweep — решить при
    реализации; инкремент по mtime, без повторных embed.
  - **Гейт полезности (обязателен):** после калибровки, на ~50 записях —
    A/B 10 запросов FTS vs гибрид; выигрыш < заметного → откатиться на FTS
    и честно зафиксировать вердикт в память. Без замера не оставлять.
slice 2: автоматика digest (фоновая задача через run-hidden.vbs).
slice 3: eligible-журнал + детектор привычек.
slice 4: resurrect (ночной, cosine >= 0.75 — порог doc_links mem).
slice 5: reflect (калибровка прогнозов, <=1 insight).
slice 6: recall-буст identity (×1.3) — только после замера эффекта slice 1.
