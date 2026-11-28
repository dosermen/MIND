# A/B-гейт MIND slice 1.5: FTS vs hybrid recall (10 queries + 1 control)
# Run: python mind-ab-gate.py   (cwd = F:\AI\MIND)
import os
import re
import subprocess
import sys

QUERIES = [
    # (query, expected_id, label)
    ("дефраг после скачивания модели", 1, "habit/defrag"),
    ("что блокирует push в github", 3, "setting/veto"),
    ("пользователь сам принимает решения", 4, "trait/autonomy"),
    ("дотошность проверять результат дважды", 9, "trait/rigor"),
    ("скептическое отношение к авторитетам", 11, "trait/skepticism"),
    ("креатив нестандартные подходы", 8, "trait/novelty"),
    ("компактный стиль общения без воды", 5, "trait/communication"),
    ("консерватизм не любит перемены", 6, "trait/conservatism"),
    ("эксперименты пробовать новое", 7, "trait/experimentalism"),
    ("inkling loop контекст", 2, "idea/inkling"),
    ("как приготовить борщ из капусты", None, "CONTROL (ожидаем мусор)"),
]

TOPK = 5


def run_recall(query, hybrid):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"   # mind.py пишет в cp1251 -> форсируем utf-8
    if hybrid:
        env["MIND_RECALL"] = "hybrid"
    else:
        env.pop("MIND_RECALL", None)
    p = subprocess.run(
        [sys.executable, "mind.py", "recall", query],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    hits = []  # [(score, id, version, type_name)]
    for line in p.stdout.splitlines():
        m = re.match(r"^\[([\d.]+)\]\s+(\d+)@v(\d+)\s+(\S+)", line)
        if m:
            hits.append((float(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)))
    mode = "?"
    m = re.search(r"mode:\s*(\S+.*)$", p.stdout, re.M)
    if m:
        mode = m.group(1).strip()
    return hits, mode, p.returncode


def rank_of(hits, target):
    for i, h in enumerate(hits, 1):
        if h[1] == target:
            return i
    return None


rows = []
print("=" * 90)
print("query                                | FTS top1/rank | HYB top1/rank | verdict")
print("=" * 90)
fts_hits_total = 0
hyb_hits_total = 0
fts_mrr = 0.0
hyb_mrr = 0.0
for q, target, label in QUERIES:
    fts, fts_mode, rc1 = run_recall(q, False)
    hyb, hyb_mode, rc2 = run_recall(q, True)
    fr = rank_of(fts, target) if target else None
    hr = rank_of(hyb, target) if target else None
    if target:
        fr5 = fr if (fr and fr <= TOPK) else None
        hr5 = hr if (hr and hr <= TOPK) else None
        fts_hits_total += 1 if fr5 else 0
        hyb_hits_total += 1 if hr5 else 0
        fts_mrr += (1.0 / fr) if fr else 0.0
        hyb_mrr += (1.0 / hr) if hr else 0.0
    f1 = f"{fts[0][3]}:{fts[0][1]}" if fts else "-"
    h1 = f"{hyb[0][3]}:{hyb[0][1]}" if hyb else "-"
    if target:
        fcol = f"{f1}/r{fr if fr else 'MISS'}"
        hcol = f"{h1}/r{hr if hr else 'MISS'}"
        if fr and not hr:
            v = "FTS only"
        elif hr and not fr:
            v = "HYB only"
        elif fr and hr:
            v = "both" + (" (hyb better)" if hr < fr else " (fts better)" if fr < hr else "")
        else:
            v = "both MISS"
    else:
        fcol = f"{f1} (n={len(fts)})"
        hcol = f"{h1} (n={len(hyb)})"
        v = "control"
    print(f"{q[:36]:36} | {fcol:13} | {hcol:13} | {v}")
    rows.append((q, target, fts, hyb, fr, hr, v))

print("=" * 90)
n = sum(1 for r in rows if r[1])
print(f"hit@{TOPK} (из {n}):  FTS {fts_hits_total}/{n}   hybrid {hyb_hits_total}/{n}")
print(f"MRR:              FTS {fts_mrr:.2f}    hybrid {hyb_mrr:.2f}")
