"""Veto settings and action evaluation."""
from __future__ import annotations

import json
import re

from .core import VETO_ACTION_MAX, ensure_index, find_root, open_db


def _veto_settings(root):
    """-> [{path, mode, executor, rules}] for every alive setting."""
    db = open_db(root)
    try:
        rows = list(db.execute(
            "SELECT path, fm_json FROM memories WHERE type='setting' AND "
            "status NOT IN ('superseded','deleted') ORDER BY path"))
    finally:
        db.close()
    out = []
    for r in rows:
        try:
            fm = json.loads(r["fm_json"] or "{}")
        except (TypeError, ValueError):
            fm = {}
        rules = []
        for rule in fm.get("veto_rules") or []:
            if isinstance(rule, dict):
                rules.append({"id": str(rule.get("id") or ""),
                              "pattern": str(rule.get("pattern") or ""),
                              "reason": str(rule.get("reason") or "")})
        out.append({"path": r["path"], "mode": str(fm.get("mode") or "off"),
                    "executor": str(fm.get("veto_executor") or "none"),
                    "rules": rules})
    return out


def cmd_veto(args):
    """Evaluate hard-veto rules for a proposed action.

    rc 0 = nothing matched (allow); rc 1 = a rule matched: BLOCK verdict
    when the setting says mode=block (executor outside the model),
    ADVICE when it says hint - the executor decides whether it is
    binding, this command only reports the fact (step 2 of the veto
    plan, user approved 26.11); rc 2 = bad usage.
    """
    root = find_root()
    ensure_index(root)
    settings = _veto_settings(root)
    action = args.action
    if not args.list and not action:
        print("veto: ACTION required (or --list)")
        return 2
    hits = []
    # compile every pattern once per run, before matching (audit 5.2.1)
    compiled = []
    for s in settings:
        if s["mode"] == "off":
            continue
        for rule in s["rules"]:
            if not rule["pattern"]:
                continue
            try:
                compiled.append((s, rule,
                                 re.compile(rule["pattern"], re.IGNORECASE)))
            except re.error:
                continue      # validate_record rejects, belt+braces
    if args.list:
        pass                  # listing never runs the patterns
    elif action and len(action) > VETO_ACTION_MAX:
        # refuse instead of truncating: a half-checked command would
        # report "allow" for text we never looked at
        print(f"veto: ACTION too long ({len(action)} > {VETO_ACTION_MAX} "
              "chars), refusing to evaluate")
        return 2
    else:
        for s, rule, rx in compiled:
            if rx.search(action or ""):
                hits.append({"setting": s["path"], "mode": s["mode"],
                             "executor": s["executor"], **rule})
    if args.list:
        data = {"settings": settings, "total_rules":
                sum(len(s["rules"]) for s in settings)}
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        if not data["total_rules"]:
            print("veto: no rules registered")
            return 0
        for s in settings:
            if s["mode"] == "off":
                continue
            print(f"{s['path']}: mode={s['mode']} executor={s['executor']} "
                  f"({len(s['rules'])} rules)")
            for rule in s["rules"]:
                print(f"  {rule['id']}: {rule['pattern']}"
                      + (f"  # {rule['reason']}" if rule["reason"] else ""))
        return 0
    blocked = [h for h in hits if h["mode"] == "block"]
    if blocked:
        verdict = "prohibit"
    elif hits:
        verdict = "advice"
    else:
        verdict = "allow"
    data = {"action": action, "verdict": verdict, "hits": hits}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if verdict == "allow" else 1
    if not hits:
        print(f"veto: allow - no hard-veto rule matched ({action!r})")
        return 0
    for h in hits:
        if h["mode"] == "block":
            head = (f"veto: PROHIBITED by {h['setting']} "
                    f"(executor: {h['executor']})")
        else:
            head = (f"veto: ADVISORY - {h['setting']} mode=hint "
                    f"(executor: {h['executor']}, not enforced)")
        print(f"{head} - rule {h['id']!r}")
        if h["reason"]:
            print(f"  why: {h['reason']}")
        print(f"  rule: {h['pattern']}")
    return 1
