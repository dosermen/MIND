---
type: idea
path: opencode-db-embed
hypothesis: "Эмбед opencode.db = «вся база агента» в векторный поиск. ЗАМЕР 28.11 (два скана): файл 8.5 ГБ, всего 1.34 B символов текста -> 155 дней телефонного embed, ~223k запросов, ~621 МБ векторов = ВЕСЬ ОБЪЁМ НЕВОЗМОЖЕН. Разбивка живого стора session_message (919M): assistant 689M / user 96.5M / compaction 66M / synthetic 60M / system 1.1M. Дайджесты сессий (titles) = 38 KB. Реалистичные скоупы: compaction-саммари 66M ~18 ч телефона (~2 дня фонового embed при гейте 9-24 и TTL лока 1 ч); user-only 96.5M ~27 ч; titles ~мгновенно. Путь БЕЗ правок mem: экспортёр SQLite (read-only, живой файл!) -> logs\\sessions\\*.md -> существующий embed_artifacts подхватывает сам (rglob *.md, art_vec). Риски: приватная схема opencode ломается на обновлении; в credential 7 строк лежат токены (экспортёру не трогать); message/ part дают \"database disk image is malformed\" на живой базе; дубль текста на диске; mem уже хранит дистиллированное = провенанс-поиск должен доказать пользу. Вопрос пользователю: какой скоуп / делать ли вообще."
scope: project
status: spark
version: 1
author: mind
importance: 3
confidence: 0.5
updated: 2026-11-28
id: 13
---
