---
type: idea
path: inkling-loop-ctx
status: lost
version: 1
author: mind
updated: 2026-11-25
importance: 4
confidence: 0.4
scope: project
hypothesis: Inkling-2-Mini loop architecture (48 KV slots) fits 12 GB at ctx >= 32k
expected: tg >= 20 t/s at ctx 32k
criterion: "2 runs, tg >= 20 t/s at ctx 32k"
cost: "8.5 GB download + 639 MB mmproj, ~30 min"
preconditions:
  - GGUF contains nextn/MTP tensors
related_mem:
  - 76@v3
resurrected: null
killed_by: "ctx sweep 25.11: 28k=8.3 t/s, 32k=7.6, 64k=1.5 - KV pinned alloc fail, working ctx only 16k"
killed_at: 2026-11-25
id: 2
---
Post-mortem seed (slice 0, imported 25.11): the full idea->hypothesis->
criterion->measurement->lost cycle happened inside one session without ever
being registered. This record closes that loop retroactively: hypothesis and
criterion are reconstructed honestly, killed_by cites the real sweep.
Deleted model, bat, provider - verdict in mem 76@v3.
