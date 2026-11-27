# DESIGN.md — MIND v0 (slice 0)

Контракт схемы, CLI и моста к mem. Источник истины для кода — этот файл.
Код: пакет `mindlib/`, shim `mind.py` и `mem-bridge.py`. Имя пакета
`mindlib` необходимо на Windows: файловая система не различает каталоги
`mind/` и `MIND/`, где хранятся записи. Python 3.10+, только stdlib во время
выполнения, 0 внешних зависимостей.

## 0. Принципы

1. md = источник истины; `mind.db` — производный индекс (auto-resync при дрейфе файлов).
2. Версии / tombstone / связи — семантика как в mem (`bump_version`, `supersedes`, `parent_id`, `related`).
3. Связь с mem однонаправленная: только чтение через MCP (`mem-bridge.py`), цитаты `id@vN`.
4. Никаких правок в `F:\AI\MEM` и `F:\AI\OLAMA\memory`.
5. ASCII в коде; русский — в md/документации.
6. Embedding (slice 1.5): каскад ТОЛЬКО remote (телефон) → FTS; локальный
   embed-server не поднимать. Recall по умолчанию FTS (`mode: fts (vectors: n/a)`),
   гибрид — opt-in `MIND_RECALL=hybrid` до A/B-гейта (~50 записей, §13).

## 1. Раскладка

```
F:\AI\MIND\
├── mind.py              # совместимый CLI shim
├── mindlib\             # ядро, MCP, veto, bridge и CLI
├── mem-bridge.py        # MemBridge: MCP-клиент к mem (единственная точка касания)
├── DESIGN.md            # этот файл
├── CONTRACT.json        # пиннинг ожидаемого контракта mem
├── MIND.md              # индекс-оглавление (генерируется sync, как MEMORY.md)
├── MIND\                # md-источник истины
│   ├── trait\  habit\  idea\  insight\  setting\
├── mind.db              # SQLite (генерируется)
├── logs\                # doctor/digest логи
├── docs\calibration.md  # локальный лист черт, gitignored (шаблон: calibration.example.md)
└── tests\               # unittest, MIND_ROOT -> tempdir
```

Корень переопределяется env `MIND_ROOT` (аналог `MEM_ROOT`) — обязательно для тестов.

## 2. Frontmatter-контракт

Парсер понимает только ограниченное подмножество YAML: строки `key: scalar`,
скаляры в одинарных/двойных кавычках, блочные списки с `- item` и вложенные
записи списка в форме `- {key: value}`. Одиночное bracket-значение вроде `[76@v3]`
и пустой список `[]` сохраняются для совместимости; flow-структуры (`[a, b]`, `{a: b}`),
anchors/aliases, merge-ключи, теги, block scalars, комментарии в конце строки
и многострочные продолжения не переписываются автоматически. При такой
конструкции `sync` сообщает `NOT rewritten, fix by hand`.

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
у insight — `falsifiable_by, reflected_on`; у setting — `block_conditions, veto_rules, override, veto_executor`.

### veto-policy: кто исполняет mode (решение 26.11)

`mode` описывает силу ЖЁСТКОЙ части; совет (hint) — отдельно.

| mode | исполнитель | что это честно значит |
|---|---|---|
| `off` | никто | записи нет в силе: ни совета, ни вето |
| `hint` | агент (LLM) | **совет**; гарантии нет, модель может промолчать |
| `block` | `veto_executor: code\|hook` | вето механическое (код/хук вне модели) |

Правило честности: `mode: block` без `veto_executor` (code|hook) — **ошибка валидации**
(агент не может исполнить блок); `veto_executor` при mode != block — тоже ошибка.
`block_conditions` — список непустых строк (что именно блокируется), человекочитаемый.
`profile`/`digest` печатают исполнителя рядом с mode, чтобы «политика» не выглядела
сильнее, чем есть.

За человекочитаемым списком стоит машинная таблица **`veto_rules`**: список
`{id, pattern, reason}`, `pattern` = regex (IGNORECASE), валидируется sync (синтаксис,
дубликаты id, наличие полей). `mind.py veto ACTION` прогоняет action по правилам
живых setting-записей (mode != off):

- rc 0 — ни одно правило не сработало (`allow`);
- rc 1 — сработало: `PROHIBITED` при mode=block (исполнитель указан), иначе
  `ADVISORY` при mode=hint (помечено как неисполняемое);
- rc 2 — нет ACTION.
`--list` печатает все правила; `--json` → `{"action", "verdict": allow|advice|prohibit,
"hits": [{setting, mode, executor, id, pattern, reason}]}`.

Смысл `veto`: исполнитель — **не модель** (шаг 2 плана 26.11). Раньше `block_conditions`
висели списком, который не читал ни один путь кода. Шаг 1 — честные ярлыки; шаг 2 — эта
команда (детерминированная, тестится); шаг 3 — **реализовано 26.11**: opencode-плагин
`F:\AI\OLAMA\.opencode\plugins\veto.ts` (id `mind-veto`, авто-дискавери `.opencode/plugins/`).

### Шаг 3: хук плагина (исполнитель вне модели)

Плагин вешает `ctx.permission.hook("evaluate")`, читает таблицу правил из
`python mind.py veto --list --json` (единственный источник истины — md-записи),
кэширует и перечитывает раз в 60 с; матчинг — in-process RegExp (латентность на
горячей ветке 0, python не спавнится). Маппинг решения 26.11:

| mode | effect хука | что происходит на деле |
|---|---|---|
| `off` | — | игнорируется |
| `hint` | `ask` | в интерактивной сессии — промпт «explicit yes» (политика как раз просит явное «да»); в агентской сессии ask авто-разрешается в allow (замерено) |
| `block` | `deny` | **реальный запрет**: команда отклоняется с `permission.rejected` и причиной из правила (проверено: запрещённый флаг не выполнился) |

- Fail-open: mind недоступен → сессия пермиссивна, ошибка пишется один раз
  (последние загруженные правила продолжают работать после первой удачной загрузки).
- Отключение: `"plugins": ["-mind-veto"]` в opencode.jsonc.
- Диагностика: `logs/veto-hook.log` — setup/вердикты/ошибки обновления правил.
- `veto_executor: hook` в seed = этот плагин; `code` = вызов `mind.py veto` из скриптов.

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
- Lifecycle-команды (`idea status`, `habit approve`, `trait approve`, ...) правят
  frontmatter in-place
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

python mind.py trait approve ID           # candidate -> active, approved_by=user, approved_at=today
python mind.py note ID (TEXT | --file F) [--side up|down]   # observation on a trait, in-place
python mind.py embed                    # fill mind_vec, remote phone only, sig increment

python mind.py profile [--scope S] [--json]
python mind.py digest [--json]
python mind.py doctor mem [--json] [--deep]
python mind.py veto ACTION [--list] [--json]   # хард-veto правила, см. §2
python mind.py serve                            # MCP stdio-сервер, см. §8а
```

Режимы вне v0 (НЕ реализуются, не регистрируются): `reflect, resurrect`.
(`serve` и `embed` — вне v0, но реализованы в slice 1 / 1.5, см. §8а и §13.)

Коды возврата: 0 — ок; 1 — drift/валидация/ничего не найдено; 2 — ошибка/недоступен mem (doctor).

`--file` может содержать frontmatter целиком: заданные значения побеждают,
недостающие обязательные берутся из флагов; тип-поля валидируются. Иначе — TEXT-аргумент
(ровно один источник: TEXT или --file, как в mem; rc=2 при обоих/ни одного).
TEXT, начинающийся с `---`, тоже парсится как frontmatter+тело (slice 1: у MCP-клиента
нет этики временных файлов, а все типы структурированы — голое тело всегда падает бы
по валидации); голый тело TEXT вставляет как тело, как раньше.
Исключения: `update --file` не принимает `id, version, supersedes, updated, path, type`
(операционная идентичность — из самой записи), а `remember`/`idea new` не могут создать
idea/habit/trait сразу в не-entry статусе (idea: `spark`, habit/trait: `candidate`) — это
обход WIP-лимита и гейта approve.

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

## 7а. Жизненный цикл trait

```
candidate --approve--> active    # только `trait approve` (approved_by=user)
                                # пересчёты value через update: статус НЕ меняется
```

Гейт на входе (26.11): `remember` не создаёт trait в не-entry статусе и `update --file`
со сменой статуса закрыт — агент не может сам активировать черту о пользователе
(та же логика честности, что veto шаг 1). Черта-кандидат НЕ попадает в profile,
НО видна в recall (поиск — это не утверждение) и в digest под «pending approval».

**Наблюдения — `note ID TEXT [--side up|down]`**: факт по черте in-place, версия не
бампается (механика `habit exception`), поле `notes: [{date, side?, text}]` индексируется
в recall. `side=up` подтверждает direction, `down` — против; 2 note с одной стороны →
digest-секция `recalc_pending` («N×side → предложи value±0.1»). Следующий `update`,
меняющий `value`, очищает учтённые notes.

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

### Гибрид (slice 1.5, opt-in `MIND_RECALL=hybrid`)

- Своя таблица `mind_vec` в mind.db (dim/model на строку, sig = sha1(content));
  `mind.py embed` добивает недостающее батчами (EMBED_BATCH 16) через remote
  `/v1/embeddings`; сироты и векторы superseded-версий чистятся при embed.
  Успешные батчи коммитятся — при упавшем remote rc=1 и resume со следующего запуска.
- Кандидаты = FTS-хиты + записи, которых FTS не нашёл, но cosine(query) >= 0.30
  (векторный добор). Формула: `0.45*bm25 + 0.20*recency + 0.10*importance + 0.25*cos`
  (веса mem, замеренные 16.11); bm25 у векторного добра = 0; cos clamp [0,1].
- Футеры честные: `hybrid` / `hybrid partial k/n` / `fts (no vectors yet)` /
  `fts (remote server down)` (телефон мёртв — молча отдаём FTS, как mem).
- Дефолт остаётся чистым FTS до A/B-гейта (§13): без замера гибрид не включается.

### MCP serve (slice 1) — `python mind.py serve`

- Новая-строка JSON-RPC 2.0 поверх stdio, тот же shape, что `mem.py serve`
  (protocol `2024-11-05`): `initialize` (serverInfo `mind`), `ping`,
  `tools/list`, `tools/call`, `notifications/*` — без ответа; мусор
  (не-объект, битый JSON, нет method) -> -32600/-32700, не падение.
- Инструменты (13): `recall profile digest show list note remember update
  forget restore veto idea habit` — всё read-only ядро + запись, кроме
  `sync/embed/link/doctor` (служебные, только CLI). Mutating-тулзы берут
  `MindLock`, как в CLI (единый mutating-набор `MUTATING`).
- Диспетчер строит argparse-Namespace и зовёт те же `cmd_*` — одна
  кодовая база, не копия логики; stdout перехватывается в текст ответа,
  rc != 0 -> `isError: true`. Исключение инструмента -> текст ошибки,
  сервер живёт (одна упавшая команда не убивает сессию).
- Root-изоляция: `find_root()` -> фолбэк `HERE` (скрипт и есть MIND), так
  что cwd opencode (чужой проект) не уводит сервер в чужое хранилище.
- TEXT `remember`/`update`, начинающийся с `---`, парсится как
  frontmatter+тело (см. §5) — иначе MCP-клиент не мог бы создать ни один
  тип: все структурированы, голое тело всегда падает по валидации.

## 9. profile и digest

`profile` — я-блок (~500 токенов), только alive:
свежие черты (по scope), active-привычки (кандидаты НЕТ), <=3 свежих insight,
счётчики parked/drafted/testing идей, текущий setting veto-policy — с явным
исполнителем (`advisory ... NOT enforced (executor: none)` для hint) и списком
hard-veto условий.

`digest` — pending одним списком (один раз в сутки, не размазано):
1. candidate-привычки/черты, ждущие approve;
2. recalc pending: >=2 note с одной стороны на trait -> «N×side, value X
   -> X±0.1»; обе стороны >=2 -> `CONFLICT ... resolve manually`; на границе
   (0.0/1.0) предложение-невозможность не выводится;
3. идеи: WIP-список, drafted с criterion (готовы к testing), parked с истёкшими preconditions — нет;
4. привычки: expires<=today или exceptions>=3;
5. doctor mem: статус контракта (drift -> warn; mem down -> warn, не фатал);
6. битые `related_mem`-цитаты (сверка показом через bridge, только при живом mem);
7. settings: mode + кто исполняет (`hint` -> `NOT enforced (executor: none)`),
   сколько hard-veto условий висит без исполнителя.

`--json` у обоих — структурированный вывод.

## 10. mem-bridge и doctor mem

`CONTRACT.json` (пиннинг):

```json
{
  "contract_version": 1,
  "pinned_at": "2026-11-25",
  "protocol": "2024-11-05",
  "serverInfo": {"name": "mem", "version": "2.0.0"},
  "tools": ["remember","recall","search","update_memory","forget","restore","list","show","link"],
  "frontmatter_required": ["type","path","status","version","author","updated"],
  "citation_pattern": "^\\d+@v\\d+$"
}
```

Пути к локальной установке mem не являются частью контракта: задайте
`MIND_MEM_PY` и `MIND_MEM_ROOT` либо создайте игнорируемый git-файл
`CONTRACT.local.json` с полями `mem_py` и `mem_root`. Переменные окружения
имеют приоритет; если пути не настроены, `doctor mem` сообщает `DOWN` и
подсказку по настройке.

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

При `initialize` bridge намеренно отправляет пустой `protocolVersion`, чтобы
сервер вернул свою фактическую версию, а не отзеркалил клиентскую. Несовпадение
с поддерживаемой версией отклоняется как protocol drift до вызова инструментов;
оно не принимается молча и `doctor mem` возвращает drift (rc 1).

Threat model: markdown-записи и входящие строки stdio считаются недоверенными
данными, способными содержать некорректный/опасный синтаксис; их парсинг и
проверки ограничивают границу доверия на стороне приложения/bridge. Bridge
разрешает только чтение удалённых записей и не исполняет содержимое markdown.
Локальный пользователь и его конфигурация (включая `CONTRACT.local.json`)
считаются доверенными; это не защита от злонамеренного владельца машины.

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
| mode=block без veto_executor | ошибка валидации sync/remember (не молчим: вето негде исполнять) |
| veto-плагин не загружен / mind недоступен | fail-open: без вето, ошибка в `logs/veto-hook.log`; после первой удачной загрузки работают последние известные правила |
| mind.db удалён/устарел | sync пересобирает из md (auto-resync, как ensure_fresh); mind_vec переживает пересборку (id из fm), сироты чистит embed |
| embed remote (телефон) недоступен | rc=1 `embed: remote unavailable after N written (M left)` — записанные батчи сохранены, resume при следующем запуске; recall → FTS `fts (remote server down)` |

## 12. Тесты

- `python -m unittest discover -s tests` из `F:\AI\MIND`.
- Каждый тест: `MIND_ROOT` -> `tempfile.TemporaryDirectory()`, `MEM` не трогаем.
- Bridge: `tests/stub_mem_server.py` — дочерний процесс, говорящий протоколом (canned responses);
  интеграционный тест против реального mem — skipUnless файл существует.
- Доктрина: тесты doctor в mind.py — skipUnless `mem-bridge.py` существует.

## 13. Вне v0 (дорожная карта)

slice 1: MCP `serve` mind + тул `profile` в opencode.jsonc. **Реализовано 26.11**
  (секция «MCP serve» в §8): 13 тулов, тот же диспетчер на cmd_*, `MIND_ROOT`
  изолирован фолбэком HERE; подключение в opencode.jsonc — следующий шаг.
slice 1.5: embed — гибридный recall. **Реализовано 26.11** (секция «Гибрид» в §8):
  - Каскад ТОЛЬКО remote (телефон 5131 через `adb forward tcp:5031 tcp:5131`) → FTS,
    как в mem; локальный embed-server НЕ поднимать (правило AGENTS.md). Телефон
    недоступен/таймаут → молча FTS, футер `mode: fts (remote server down)`.
  - Своя таблица `mind_vec` в mind.db (dim 1024, Q8_0) — в mem_vec НЕ лезть,
    однонаправленность mind→mem сохраняется.
  - Команды: `mind.py embed` (добивка недоставленных векторов) + гибридный
    `recall` с весами, старт с mem-замеренных 0.45/0.20/0.10/0.25
    (bm25/recency/importance/cosine), FTS-веса v0 остаются fallback'ом.
  - Sweep: отдельная задача (hidden vbs) либо шаг в Mem-Sweep — решить при
    реализации (embed пока ручной); инкремент по sig, без повторных embed.
  - **Гейт полезности (обязателен, ОСТАЁТСЯ):** после калибровки, на ~50 записях —
    A/B 10 запросов FTS vs гибрид; выигрыш < заметного → откатиться на FTS
    и честно зафиксировать вердикт в память. Без замера не оставлять
    (поэтому гибрид opt-in `MIND_RECALL=hybrid`, дефолт — FTS).
slice 2: автоматика digest (фоновая задача через run-hidden.vbs).
slice 3: eligible-журнал + детектор привычек.
slice 4: resurrect (ночной, cosine >= 0.75 — порог doc_links mem).
slice 5: reflect (калибровка прогнозов, <=1 insight).
slice 6: recall-буст identity (×1.3) — только после замера эффекта slice 1.

## 14. Гипотезы аудита 26.11 — вердикты (AUDIT.md §9)

- **H1 (stale-lock vs длинный rebuild) — риск не подтвердился.** Probe:
  heartbeat живого владельца держит mtime свежим, второй процесс видит
  held и получает BUSY; steal срабатывает только на мёртвом владельце.
- **H2 (os.replace-steal при открытом fd, Windows) — steal падает
  безопасно.** Пока второй процесс держит lockfile открытым, `os.replace`
  даёт PermissionError (WinError 32) → `except OSError` → без порчи
  lockfile и без захвата. Probe: `tests/h2-lock-open-handle-probe.py`.
- **H3 (protocolVersion mismatch) — закрыт тестом.** Reject до вызова
  инструментов, doctor → `contract: DRIFT`, rc=1
  (`test_protocol_mismatch_is_reported_as_drift`: `0.1`, `1999-01-01`).
- **H4 (embed/hybrid покрытие) — подтверждено.** Vector-only ветка
  косинуса (FTS не видит запись — гибрид находит), fallback'и
  no-vectors / remote-down / partial и порядок кандидатов
  (`test_hybrid_ranks_by_cosine_among_vector_only`) — в тестах.
