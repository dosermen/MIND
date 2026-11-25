---
type: habit
path: defrag-after-download
status: candidate
version: 1
author: mind
updated: 2026-11-25
importance: 4
confidence: 0.3
scope: agent
trigger: after every model/module/build download
action: "path to logs\\wc-queue.txt + schtasks /Run /TN WinContig-Defrag (HDD only, never C:/D:)"
related_mem: [76@v3]
id: 1
---
Legacy import from AGENTS.md rules (source: legacy, confidence 0.3).
Candidate until the user approves it via `mind.py habit approve`.
