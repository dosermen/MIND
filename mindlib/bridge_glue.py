"""Configuration and diagnostics for the read-only mem bridge."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import date
from pathlib import Path

from .core import CITATION_RE, HABIT_EXCEPTION_LIMIT, HERE, ensure_index, find_root, open_db

_bridge_mod = None


def load_bridge():
    global _bridge_mod
    if _bridge_mod is not None:
        return _bridge_mod
    import importlib.util
    path = HERE / "mem-bridge.py"
    if not path.is_file():
        raise RuntimeError(f"mem-bridge.py not found: {path}")
    spec = importlib.util.spec_from_file_location("mem_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _bridge_mod = mod
    return mod


def _resolve_mem_paths(root):
    """Resolve the host-specific mem executable/root without pinning them."""
    local_path = Path(root) / "CONTRACT.local.json"
    local = {}
    env_py = os.environ.get("MIND_MEM_PY")
    env_root = os.environ.get("MIND_MEM_ROOT")
    if not (env_py and env_root) and local_path.exists():
        try:
            local = json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return None, None, f"bad CONTRACT.local.json: {e}"
        if not isinstance(local, dict):
            return None, None, "bad CONTRACT.local.json: object expected"
    mem_py = env_py or local.get("mem_py")
    mem_root = env_root or local.get("mem_root")
    if not isinstance(mem_py, str) or not mem_py.strip() or \
            not isinstance(mem_root, str) or not mem_root.strip():
        return None, None, (
            "missing mem paths; set MIND_MEM_PY and MIND_MEM_ROOT or "
            "configure CONTRACT.local.json")
    return mem_py, mem_root, None


def _mem_contract(root, timeout=20.0, deep=False):
    """-> dict(state=ok|drift|down, report=?, deep_hits=?, broken=?)."""
    out = {"state": "down", "report": None, "deep_hits": [],
           "broken": None, "detail": ""}
    # one wall-clock budget for the WHOLE probe: without it a hanging mem
    # can burn timeout x (handshake + deep + up to 20 show) ~ minutes
    deadline = time.monotonic() + float(timeout)

    def left():
        return max(1.0, deadline - time.monotonic())

    cp = root / "CONTRACT.json"
    if not cp.is_file():
        out["detail"] = "no CONTRACT.json"
        return out
    try:
        contract = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out["detail"] = f"bad CONTRACT.json: {e}"
        return out
    mem_py, mem_root, path_error = _resolve_mem_paths(root)
    if path_error:
        out["detail"] = path_error
        return out
    try:
        mod = load_bridge()
    except Exception as e:
        out["detail"] = f"bridge load failed: {e}"
        return out
    br = mod.MemBridge(mem_py, mem_root, timeout=timeout)
    try:
        br.timeout = min(br.timeout, left())
        br.start()
        br.handshake()
        # mem_root sanity: the child prints "mem serve: root=..." to
        # stderr; a mismatch means it ignored MEM_ROOT and serves the
        # wrong memory (review MED)
        want_root = str(Path(mem_root)).rstrip("/\\")
        # snapshot: the drain thread mutates the deque (review m9)
        for line in list(br.stderr_tail):
            if line.startswith("mem serve: root="):
                got_root = line.split("=", 1)[1].strip().rstrip("/\\")
                if want_root and got_root.lower() != want_root.lower():
                    out["detail"] = (f"mem serves root {got_root!r}, "
                                     f"contract pins {want_root!r}")
                    return out
                break
        br.timeout = min(br.timeout, left())
        report = br.check_contract(contract)
        out["report"] = report
        out["state"] = "ok" if report["ok"] else "drift"
        if deep:
            try:
                br.timeout = min(br.timeout, left())
                hits = br.recall("test", limit=3)
                out["deep_hits"] = [h["citation"] for h in hits]
                # citation shape comes from the contract pin, not from a
                # second hardcoded regex (audit 2.2.2); CITATION_RE only
                # backstops a contract that does not pin a pattern
                pat = contract.get("citation_pattern")
                try:
                    cre = re.compile(pat) if isinstance(pat, str) and pat \
                        else CITATION_RE
                except re.error:
                    cre = CITATION_RE  # check_contract already reported it
                for h in hits:
                    if not cre.match(h["citation"]):
                        report["drifts"].append(
                            f"bad citation shape: {h['citation']!r}")
                        report["ok"] = False
                        out["state"] = "drift"
                # frontmatter_required against a live record: the contract
                # claims mem records carry these fields - verify one
                # (audit 2.2.2)
                if hits:
                    br.timeout = min(br.timeout, left())
                    sample = br.show(hits[0]["citation"])
                    for d in getattr(mod, "check_contract_fields",
                                     lambda c, t: [])(contract, sample):
                        report["drifts"].append(d)
                        report["ok"] = False
                        out["state"] = "drift"
            except Exception as e:
                # transport failure (spawn/EOF/timeout) means mem is gone:
                # DOWN rc=2, not a contract drift (review MAJOR-9)
                if isinstance(e, getattr(mod, "TransportError", ())):
                    raise
                report["drifts"].append(f"recall failed: {e}")
                report["ok"] = False
                out["state"] = "drift"
        # broken related_mem citations (unique, capped)
        db = open_db(root)
        try:
            cites = set()
            for r in db.execute(
                    "SELECT related_mem FROM memories WHERE "
                    "status NOT IN ('superseded','deleted') AND "
                    "related_mem NOT IN ('[]','')"):
                try:
                    val = json.loads(r["related_mem"] or "[]")
                except (TypeError, ValueError):
                    continue
                for c in (val if isinstance(val, list) else [val]):
                    if c:
                        cites.add(str(c))
        finally:
            db.close()
        broken = []
        for c in sorted(cites)[:20]:
            if time.monotonic() > deadline:
                break  # budget exhausted: report what we checked
            br.timeout = min(br.timeout, left())
            if br.show(c) is None:
                broken.append(c)
        out["broken"] = broken
    except Exception as e:
        if isinstance(e, getattr(mod, "ProtocolVersionError", ())):
            out["state"] = "drift"
            out["detail"] = str(e)
            out["report"] = {
                "ok": False, "drifts": [str(e)],
                "server": getattr(br, "server_info", {}),
                "protocol": getattr(br, "protocol_version", None),
                "tools": [],
            }
        else:
            out["state"] = "down"
            out["detail"] = str(e)
    finally:
        br.close()
    return out


def cmd_digest(args):
    root = find_root()
    ensure_index(root)
    today = date.today()
    db = open_db(root)
    try:
        pending = [{"kind": r["type"], "id": r["id"], "path": r["path"],
                    "status": r["status"]} for r in db.execute(
            "SELECT * FROM memories WHERE status='candidate' AND "
            "type IN ('trait','habit') ORDER BY type, path")]
        wip = [{"id": r["id"], "path": r["path"], "status": r["status"]}
               for r in db.execute(
            "SELECT * FROM memories WHERE type='idea' AND "
            "status IN ('drafted','testing') ORDER BY status, path")]
        ready = []
        for r in db.execute(
                "SELECT * FROM memories WHERE type='idea' AND "
                "status='drafted' ORDER BY path"):
            try:
                fm = json.loads(r["fm_json"] or "{}")
            except (TypeError, ValueError):
                fm = {}
            if str(fm.get("criterion") or "").strip():
                ready.append({"id": r["id"], "path": r["path"]})
        try:
            parked_n = int(db.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE type='idea' AND "
                "status='parked'").fetchone()["n"])
        except sqlite3.OperationalError:
            parked_n = 0
        # column 'criterion' does not exist -> fm_json query instead
        revision = []
        for r in db.execute(
                "SELECT * FROM memories WHERE type='habit' AND "
                "status='active'"):
            try:
                fm = json.loads(r["fm_json"] or "{}")
            except (TypeError, ValueError):
                fm = {}
            reasons = []
            exp = str(fm.get("expires") or "")
            if exp and exp <= today.isoformat():
                reasons.append(f"expired {exp}")
            exc = fm.get("exceptions")
            if isinstance(exc, list) and len(exc) >= HABIT_EXCEPTION_LIMIT:
                reasons.append(f"{len(exc)} exceptions")
            if reasons:
                revision.append({"id": r["id"], "path": r["path"],
                                 "reasons": reasons})
        # recalc pending: 2 same-side notes = the calibration rule's
        # trigger; notes are cleared by the next value change
        recalc = []
        for r in db.execute(
                "SELECT * FROM memories WHERE type='trait' AND "
                "status NOT IN ('superseded','deleted')"):
            try:
                fm = json.loads(r["fm_json"] or "{}")
            except (TypeError, ValueError):
                fm = {}
            notes = fm.get("notes")
            if not isinstance(notes, list):
                continue
            sides = {"up": 0, "down": 0}
            for n in notes:
                if isinstance(n, dict) and n.get("side") in sides:
                    sides[n["side"]] += 1
            if sides["up"] < 2 and sides["down"] < 2:
                continue
            try:
                val = float(fm.get("value"))
            except (TypeError, ValueError):
                continue
            if sides["up"] >= 2 and sides["down"] >= 2:
                # contradictory evidence must not masquerade as one-sided
                recalc.append({"id": r["id"], "path": r["path"],
                               "side": "conflict", "up": sides["up"],
                               "down": sides["down"], "value": val,
                               "suggested": None})
                continue
            side = "down" if sides["down"] >= 2 else "up"
            delta = 0.1 if side == "up" else -0.1
            suggested = round(min(1.0, max(0.0, val + delta)), 2)
            if suggested == val:
                continue   # clamped at a bound: the reminder would be a no-op
            recalc.append({"id": r["id"], "path": r["path"], "side": side,
                           "count": sides[side], "value": val,
                           "suggested": suggested})
        # who enforces each setting (honest label, user 26.11): an
        # advisory mode with a hard-veto list means nobody executes it
        settings = []
        for r in db.execute(
                "SELECT * FROM memories WHERE type='setting' AND "
                "status NOT IN ('superseded','deleted') ORDER BY path"):
            try:
                fm = json.loads(r["fm_json"] or "{}")
            except (TypeError, ValueError):
                fm = {}
            settings.append({
                "id": r["id"], "path": r["path"],
                "mode": fm.get("mode"),
                "executor": fm.get("veto_executor") or "none",
                "conditions": len(fm.get("block_conditions") or [])})
    finally:
        db.close()

    mem = _mem_contract(root, timeout=20.0, deep=False)

    data = {"date": today.isoformat(), "pending": pending,
            "ideas": {"wip": wip, "ready_for_testing": ready,
                      "parked": parked_n},
            "habit_revision": revision,
            "recalc_pending": recalc,
            "settings": settings,
            "mem": {"state": mem["state"],
                    "detail": mem["detail"] or None,
                    "drifts": (mem["report"] or {}).get("drifts", [])},
            "broken_citations": mem["broken"]}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"MIND digest ({data['date']})")
    print(f"1. pending approval ({len(pending)})")
    for p in pending:
        print(f"   {p['kind']} {p['id']} {p['path']} [{p['status']}]")
    if not pending:
        print("   (clean)")
    print(f"2. recalc pending ({len(recalc)})")
    for rc_ in recalc:
        if rc_["side"] == "conflict":
            print(f"   trait {rc_['id']} {rc_['path']}: CONFLICT "
                  f"{rc_['up']}up vs {rc_['down']}down -> value "
                  f"{rc_['value']}, resolve manually")
        else:
            print(f"   trait {rc_['id']} {rc_['path']}: {rc_['count']}x"
                  f"{rc_['side']} -> value {rc_['value']} -> "
                  f"suggest {rc_['suggested']}")
    if not recalc:
        print("   (clean)")
    print(f"3. ideas: wip {len(wip)}, ready-for-testing {len(ready)}, "
          f"parked {parked_n}")
    for w in wip:
        print(f"   {w['id']} {w['path']} [{w['status']}]")
    print(f"4. habit revision due: {len(revision)}")
    for h in revision:
        print(f"   {h['id']} {h['path']}: {', '.join(h['reasons'])}")
    st = mem["state"]
    if st == "ok":
        print("5. mem contract: OK")
    elif st == "drift":
        print("5. mem contract: DRIFT")
        for d in data["mem"]["drifts"]:
            print(f"   - {d}")
    else:
        print(f"5. mem contract: DOWN ({mem['detail']})")
    broken = mem["broken"]
    if broken is None:
        print("6. broken citations: (mem unavailable, skipped)")
    elif broken:
        print(f"6. broken citations: {', '.join(broken)}")
    else:
        print("6. broken citations: none")
    if settings:
        print(f"7. settings ({len(settings)})")
        for s in settings:
            if s["mode"] == "hint":
                state = (f"advisory, hard-veto {s['conditions']} "
                         f"NOT enforced (executor: {s['executor']})")
            elif s["mode"] == "block":
                state = (f"enforced by {s['executor']}, "
                         f"hard-veto {s['conditions']}")
            else:
                state = "off: no advice, no veto"
            print(f"   {s['path']}={s['mode']} ({state})")
    return 0


def cmd_doctor(args):
    root = find_root()
    res = _mem_contract(root, timeout=30.0, deep=bool(args.deep))
    report = res["report"] or {}
    if args.json:
        out = {"state": res["state"], "detail": res["detail"] or None,
               "report": report, "deep_hits": res["deep_hits"],
               "broken_citations": res["broken"]}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if res["state"] == "down":
            print(f"mem bridge: DOWN ({res['detail']})")
        else:
            print(f"mem: {report.get('server')} "
                  f"protocol={report.get('protocol')}")
            print(f"tools ({len(report.get('tools', []))}): "
                  + ", ".join(report.get("tools", [])))
            if res["state"] == "ok":
                print("contract: OK")
            else:
                print("contract: DRIFT")
                for d in report.get("drifts", []):
                    print(f"  - {d}")
            if args.deep:
                print(f"deep recall: {len(res['deep_hits'])} hits "
                      f"({', '.join(res['deep_hits']) or '-'})")
            if res["broken"]:
                print(f"broken citations: {', '.join(res['broken'])}")
    if res["state"] == "down":
        return 2
    return 0 if res["state"] == "ok" else 1
