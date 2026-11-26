---
type: setting
path: veto-policy
status: active
version: 1
author: user
updated: 2026-11-25
importance: 5
confidence: 1.0
scope: all
mode: hint
veto_executor: none
hint_style: suggest + explain
block_conditions: [push to GitHub without explicit yes (ban per AGENTS.md), "permanently forbidden flags (GGML_VK_ALLOW_GRAPHICS_QUEUE, GGML_VK_DISABLE_INTEGER_DOT_PRODUCT, GGML_VK_FA_SPARSE_DISABLE)", PC shutdown / keeper 5023 without instruction]
override: user can override any block with an explicit no
id: 3
---
Veto policy (user decision 25.11): default mode is hint - suggest, advise,
point out risks, but do not block. Blocking is allowed only in the
extraordinary cases listed in block_conditions, and always with an
explanation of why the system is stopping.
