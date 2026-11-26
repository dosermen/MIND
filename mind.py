#!/usr/bin/env python3
"""MIND - cognitive layer: traits, habits, ideas, insights (slice 0).

Contract: DESIGN.md (this directory). Principles copied from mem:
  * md files under MIND\\ are the source of truth; mind.db is derived
    (full rebuild on sync, auto-resync on drift);
  * versions = new file + supersedes (old -> superseded), tombstone
    delete, citations id@vN, related links without a version bump;
  * one-way read-only bridge to mem via mem-bridge.py (MCP client).
stdlib only, single file (mem principle). ASCII in code.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
import traceback
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

TYPES = ("trait", "habit", "idea", "insight", "setting")
SCOPES = ("agent", "project", "companion", "all")  # "all" only for setting
DEAD = ("superseded", "deleted")

STATUS_MATRIX = {
    "trait": ("candidate", "active", "superseded", "deleted"),
    "habit": ("candidate", "active", "deprecated", "superseded", "deleted"),
    "idea": ("spark", "drafted", "testing", "won", "lost", "parked",
             "superseded", "deleted"),
    "insight": ("active", "superseded", "deleted"),
    "setting": ("active", "superseded", "deleted"),
}

TYPE_REQUIRED = {
    "trait": ("scope", "value", "direction", "falsified_when"),
    "habit": ("scope", "trigger", "action"),
    "idea": ("hypothesis",),
    "insight": ("summary", "prediction"),
    "setting": ("mode",),
}

IDEA_TRANSITIONS = {
    "spark": ("drafted", "parked"),
    "drafted": ("testing", "parked"),
    "testing": ("won", "lost"),
    "parked": ("drafted", "testing"),
    "won": (),
    "lost": (),
}
WIP_MAX = 7
HABIT_EXPIRE_DAYS = 90
HABIT_EXCEPTION_LIMIT = 3

CITATION_RE = re.compile(r"^\d+@v\d+$")
RECALL_LIMIT = 10
RECALL_BUDGET = 6000
RECENCY_DAYS = 30.0
W_BM25, W_REC, W_IMP = 0.60, 0.25, 0.15

# slice 1.5: embed + hybrid recall (DESIGN 13). Remote-only on purpose:
# the phone behind `adb forward tcp:5031 tcp:5131` (AGENTS.md - never
# spawn a local embed-server). Weights are mem's measured hybrid set.
EMBED_URL_DEFAULT = "http://127.0.0.1:5031/v1/embeddings"
EMBED_MODEL_DEFAULT = "Qwen3-Embedding-0.6B"
EMBED_TIMEOUT_S = 60
EMBED_BATCH = 16
W_HYB_BM25, W_HYB_REC, W_HYB_IMP, W_HYB_COS = 0.45, 0.20, 0.10, 0.25
HYBRID_MIN_COS = 0.30   # vector-only candidates below this are ignored

# per-type default status: a blind "active" is invalid for idea/trait/habit
# (review: remember --type idea always failed on the default)
DEFAULT_STATUS = {
    "trait": "candidate",
    "habit": "candidate",
    "idea": "spark",
    "insight": "active",
    "setting": "active",
}

PREVIEW_FIELDS = {
    "trait": ("value", "direction"),
    "habit": ("trigger", "action"),
    "idea": ("hypothesis",),
    "insight": ("summary",),
    "setting": ("mode",),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY,
  file TEXT UNIQUE NOT NULL,
  path TEXT, type TEXT, status TEXT,
  content TEXT, body TEXT,
  version INTEGER, parent_id INTEGER,
  importance INTEGER, confidence REAL, scope TEXT,
  author TEXT, session TEXT,
  created_at TEXT, updated_at TEXT,
  related_mind TEXT, related_mem TEXT,
  fm_json TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(search_text);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- slice 1.5: own vector store (dim/model recorded per row; NEVER mem_vec)
CREATE TABLE IF NOT EXISTS mind_vec (
  mem_id INTEGER PRIMARY KEY,
  sig TEXT NOT NULL,
  dim INTEGER NOT NULL,
  model TEXT,
  vec BLOB NOT NULL,
  updated TEXT
);
"""

_bridge_mod = None


# ---------------------------------------------------------------------------
# frontmatter: minimal YAML subset (scalars, flow lists, block flow-dicts)
# ---------------------------------------------------------------------------

def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("\"", "'"):
        inner = s[1:-1]
        if s[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return s


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("\"", "'"):
        return _unquote(v)
    low = v.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[+-]?\d+", v):
        return int(v)
    if re.fullmatch(r"[+-]?\d*\.\d+", v):
        return float(v)
    return v


def _split_top(s):
    """Split on commas at depth 0, respecting quotes and backslash escapes.

    Roundtrip review (26.11): _fmt writes `\\"` inside double quotes, but
    the old char-loop had no escape handling - the escaped quote closed
    the quote state and a later comma SPLIT the value, silently cutting
    note text like `5" bolt, tight` (probe-confirmed, persisted by sync).
    """
    parts, depth, cur, q = [], 0, "", None
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if q:
            if q == '"' and ch == "\\" and i + 1 < n:
                cur += ch + s[i + 1]   # keep the escape pair for _unquote
                i += 2
                continue
            cur += ch
            if ch == q:
                q = None
            i += 1
            continue
        if ch in ("\"", "'"):
            q = ch
            cur += ch
            i += 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts]


def _flow_item(s):
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        d = {}
        for kv in _split_top(s[1:-1]):
            k, _, v = kv.partition(":")
            d[k.strip()] = _scalar(v)
        return d
    return _scalar(s)


def parse_frontmatter(text):
    """-> (fm dict, body str). No frontmatter -> ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)",
                 text, re.S)
    if not m:
        return {}, text
    return _parse_yaml(m.group(1)), text[m.end():]


def _parse_yaml(block):
    out = {}
    cur_list = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indented = raw[0] in (" ", "\t")
        stripped = raw.strip()
        if indented and cur_list is not None:
            if stripped.startswith("- "):
                cur_list.append(_flow_item(stripped[2:]))
            elif stripped == "-":
                cur_list.append(None)
            continue
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        k, v = k.strip(), v.strip()
        if v == "":
            out[k] = []
            cur_list = out[k]
            continue
        cur_list = None
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            out[k] = [_scalar(p) for p in _split_top(inner)] if inner else []
        else:
            out[k] = _scalar(v)
    return out


def _fmt(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\n", " ")
    needs = (s == "" or s != s.strip()
             or any(c in s for c in ":#\"'[],{}")
             or re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)", s)
             or s.lower() in ("null", "true", "false", "~"))
    if needs:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def dump_frontmatter(d):
    lines = ["---"]
    for k, v in d.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            elif all(not isinstance(x, (dict, list)) for x in v):
                lines.append(f"{k}: [" + ", ".join(_fmt(x) for x in v) + "]")
            else:
                lines.append(f"{k}:")
                for x in v:
                    if isinstance(x, dict):
                        inner = ", ".join(f"{kk}: {_fmt(vv)}"
                                          for kk, vv in x.items())
                        lines.append(f"  - {{{inner}}}")
                    elif isinstance(x, list):
                        lines.append("  - [" + ", ".join(_fmt(y)
                                                         for y in x) + "]")
                    else:
                        lines.append(f"  - {_fmt(x)}")
        elif isinstance(v, dict):
            inner = ", ".join(f"{kk}: {_fmt(vv)}" for kk, vv in v.items())
            lines.append(f"{k}: {{{inner}}}")
        else:
            lines.append(f"{k}: {_fmt(v)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def write_md(path, fm, body):
    """Atomic md write (tmp + os.replace, mem principle)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = body or ""
    if body and not body.endswith("\n"):
        body += "\n"
    text = dump_frontmatter(fm) + body
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# root / db / index
# ---------------------------------------------------------------------------

def _is_mind_root(p):
    """A real project root, not just any directory with a MIND subdir.

    Review (26.11): the bare `(p / "MIND").is_dir()` marker matched a
    workspace root that merely CONTAINS a project named MIND - running
    from F:\\AI\\OLAMA walked up to F:\\AI, built a second store there
    and sprayed records outside the project. The records marker now
    requires MIND/ to look like a records tree (a type subdir), and a
    bare MIND/ without a database or index no longer qualifies.
    """
    if (p / "MIND.md").is_file() or (p / "mind.db").is_file():
        return True
    md = p / "MIND"
    if md.is_dir():
        return any((md / t).is_dir()
                   for t in ("trait", "habit", "idea", "insight", "setting"))
    return False


def find_root():
    env = os.environ.get("MIND_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    for p in (cwd, *cwd.parents):
        if _is_mind_root(p):
            return p
    return HERE  # script IS the project (mem deliberately does not do this)


def open_db(root):
    db = sqlite3.connect(str(root / "mind.db"), timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


LOCK_STALE_SECONDS = 300

# in-process reentrancy: mutations hold the lock via main(), while
# ensure_index/_run_sync called from READ commands must take it too
# (review M1: reads rebuilt the store with no lock at all)
_LOCK_HELD = False


class MindLock:
    """Single-writer lock for mutations (review MED).

    md stays the source of truth, but two concurrent writers racing on
    write_md + full rebuild can interleave into a torn index. Advisory
    lockfile next to mind.db; a lock older than LOCK_STALE_SECONDS is
    stolen (crashed writer). Reentrant within this process: nested
    acquisitions (mutation -> _run_sync) are no-ops.
    """

    def __init__(self, root):
        self.path = Path(root) / "mind.lock"
        self.acquired = False
        self._owner = None
        self._heartbeat_stop = None
        self._heartbeat_thread = None

    def _heartbeat(self, interval=30.0):
        while not self._heartbeat_stop.wait(interval):
            try:
                if self.path.read_text(encoding="ascii") != self._owner:
                    return
                os.utime(str(self.path), None)
            except (OSError, UnicodeDecodeError):
                return

    def __enter__(self):
        global _LOCK_HELD
        if _LOCK_HELD:
            return self  # reentrant: outermost holder owns the file
        for _ in range(2):
            try:
                fd = os.open(str(self.path),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                owner = hashlib.sha256(
                    f"{os.getpid()}-{time.time_ns()}".encode("ascii")
                ).hexdigest()
                os.write(fd, owner.encode("ascii"))
                os.close(fd)
                self._owner = owner
                self.acquired = True
                _LOCK_HELD = True
                self._heartbeat_stop = threading.Event()
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat,
                    name="mind-lock-heartbeat",
                    daemon=True,
                )
                self._heartbeat_thread.start()
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue  # vanished, retry the create
                if age > LOCK_STALE_SECONDS:
                    # steal atomically: rename wins for exactly one
                    # contender; a loser gets OSError -> retry (review m2)
                    try:
                        os.replace(str(self.path),
                                   str(self.path) + ".stale")
                        (Path(str(self.path) + ".stale")).unlink()
                    except OSError:
                        pass
                    continue  # retry create once
                raise RuntimeError(
                    f"another mind writer holds {self.path.name} "
                    f"({int(age)}s old)")
        raise RuntimeError(f"cannot acquire {self.path.name}")

    def __exit__(self, *exc):
        global _LOCK_HELD
        if self.acquired:
            if self._heartbeat_stop is not None:
                self._heartbeat_stop.set()
                if self._heartbeat_thread is not None:
                    self._heartbeat_thread.join(timeout=1.0)
            try:
                if self.path.read_text(encoding="ascii") == self._owner:
                    self.path.unlink()
            except OSError:
                pass
            self.acquired = False
            _LOCK_HELD = False
        return False


def _md_facts(root):
    """-> (newest_mtime, file_count, total_bytes); (None, 0, 0) if absent."""
    md_dir = root / "MIND"
    if not md_dir.is_dir():
        return None, 0, 0
    newest, count, size = 0.0, 0, 0
    for f in md_dir.rglob("*.md"):
        count += 1
        try:
            st = f.stat()
            newest = max(newest, st.st_mtime)
            size += st.st_size
        except OSError:
            pass
    return newest, count, size


def ensure_index(root):
    """Auto-resync on drift: missing db, newer md, OR deleted md.

    mtime alone cannot see a manual deletion (review MED): the file is
    gone, every remaining mtime is older than the db. Both facts (count
    and newest mtime) are stamped into meta at each sync and compared
    exactly - no fudge window (audit: writes <1 ms after the rebuild
    used to slip through the old +0.001 tolerance).
    """
    db_path = root / "mind.db"
    # a READ command rebuilding the store is still a writer (review M1):
    # take the same lock mutations use; reentrant when we already hold it
    with MindLock(root):
        if not db_path.is_file():
            _run_sync(root, lenient=True, quiet=True)
            return
        newest, count, size = _md_facts(root)
        stored_count = stored_newest = stored_size = None
        try:
            db = open_db(root)
            try:
                row = db.execute(
                    "SELECT k, v FROM meta WHERE k IN ('md_count',"
                    " 'md_newest', 'md_size')").fetchall()
                vals = {r["k"]: r["v"] for r in row}
                stored_count = int(vals["md_count"]) if "md_count" in vals \
                    else None
                stored_newest = float(vals["md_newest"]) \
                    if "md_newest" in vals else None
                stored_size = int(vals["md_size"]) if "md_size" in vals \
                    else None
            finally:
                db.close()
        except (sqlite3.Error, OSError, TypeError, ValueError):
            stored_count = stored_newest = stored_size = None
        drifted = (
            stored_count is None or stored_count != count
            or stored_newest is None
            or (newest if newest is not None else 0.0) != stored_newest
            or stored_size is None or stored_size != size
        )
        if drifted:
            _run_sync(root, lenient=True, quiet=True)


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_record(fm, rel):
    """-> (errors, warns) for one record's frontmatter."""
    errors, warns = [], []
    typ = fm.get("type")
    if typ not in TYPES:
        errors.append(f"{rel}: missing/unknown type {typ!r}")
        return errors, warns
    st = fm.get("status")
    if st not in STATUS_MATRIX[typ]:
        errors.append(f"{rel}: invalid status {st!r} for type {typ}")
        return errors, warns
    if typ == "setting":
        sc = fm.get("scope")
        if sc is not None and sc not in SCOPES:
            errors.append(f"{rel}: invalid scope {sc!r}")
    else:
        if fm.get("scope") not in ("agent", "project", "companion"):
            errors.append(f"{rel}: invalid/missing scope "
                          f"{fm.get('scope')!r} (agent|project|companion)")
    try:
        imp = int(fm.get("importance", 3))
        if not 1 <= imp <= 5:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{rel}: importance must be 1..5")
    try:
        conf = float(fm.get("confidence", 0.5))
        if not 0.0 <= conf <= 1.0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{rel}: confidence must be 0..1")
    for k in TYPE_REQUIRED.get(typ, ()):
        v = fm.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            errors.append(f"{rel}: type {typ} requires field '{k}'")
    if typ == "trait":
        try:
            val = float(fm.get("value"))
            if not 0.0 <= val <= 1.0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{rel}: trait value must be a number 0..1")
    if typ == "idea":
        crit = str(fm.get("criterion") or "").strip()
        if st in ("testing", "won", "lost") and not crit:
            errors.append(f"{rel}: idea status={st} requires 'criterion'")
        if st == "lost" and not str(fm.get("killed_by") or "").strip():
            errors.append(f"{rel}: idea status=lost requires 'killed_by'")
    if typ == "setting":
        mode = fm.get("mode")
        if mode not in ("off", "hint", "block"):
            errors.append(f"{rel}: setting mode must be off|hint|block")
        # honesty rule (user decision 26.11): 'block' must name an
        # executor that lives OUTSIDE the model - otherwise the setting
        # claims an enforcement that does not exist
        if mode == "block":
            ex = fm.get("veto_executor")
            if ex not in ("code", "hook"):
                errors.append(
                    f"{rel}: mode 'block' requires veto_executor "
                    f"code|hook (got {ex!r}); an LLM cannot enforce a "
                    "block - use mode 'hint' for advisory only")
        elif fm.get("veto_executor") not in (None, "", "none"):
            errors.append(
                f"{rel}: veto_executor={fm.get('veto_executor')!r} only "
                "meaningful with mode 'block'")
        bc = fm.get("block_conditions")
        if bc is not None:
            if not isinstance(bc, list):
                errors.append(f"{rel}: block_conditions must be a list")
            elif any(not str(x).strip() for x in bc):
                errors.append(f"{rel}: block_conditions has an empty entry")
        # machine-checkable rules behind block_conditions: `mind.py veto`
        # evaluates these, so a malformed one is a broken promise
        vr = fm.get("veto_rules")
        if vr is not None:
            if not isinstance(vr, list):
                errors.append(f"{rel}: veto_rules must be a list")
            else:
                seen_ids = set()
                for rule in vr:
                    if not isinstance(rule, dict):
                        errors.append(f"{rel}: veto_rules entry must be "
                                      "a mapping {id, pattern, reason}")
                        continue
                    rid = str(rule.get("id") or "").strip()
                    pat = str(rule.get("pattern") or "")
                    if not rid or not pat:
                        errors.append(f"{rel}: veto_rules entry needs "
                                      "id and pattern")
                        continue
                    if rid in seen_ids:
                        errors.append(f"{rel}: duplicate veto rule id "
                                      f"{rid!r}")
                    seen_ids.add(rid)
                    try:
                        re.compile(pat, re.IGNORECASE)
                    except re.error as e:
                        errors.append(f"{rel}: veto_rules[{rid}] bad "
                                      f"pattern: {e}")
    for c in (fm.get("related_mem") or []) if isinstance(
            fm.get("related_mem"), list) else []:
        if c and not CITATION_RE.match(str(c)):
            warns.append(f"{rel}: suspicious citation {c!r} in related_mem")
    return errors, warns


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def _build_search_text(fm, body):
    parts = [str(body or "")]

    def flat(v):
        if isinstance(v, list):
            return " ".join(flat(x) for x in v)
        if isinstance(v, dict):
            return " ".join(flat(x) for x in v.values())
        if v is None:
            return ""
        return str(v)

    for k, v in fm.items():
        if k == "id":
            continue
        s = flat(v)
        if s:
            parts.append(s)
    return "\n".join(p for p in parts if p)


def write_index(db, root):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = ["# MIND.md - index", f"generated: {now}", ""]
    for typ in TYPES:
        rows = list(db.execute(
            "SELECT * FROM memories WHERE type=? AND status != 'deleted' "
            "ORDER BY path", (typ,)))
        if not rows:
            continue
        lines.append(f"## {typ} ({len(rows)})")
        for r in rows:
            lines.append(f"- **{r['path']}** {r['id']}@v{r['version']} "
                         f"[{r['status']}] ({r['updated_at']}) "
                         f"imp{r['importance']}")
        lines.append("")
    # atomic like write_md (review m3): a kill mid-write must not leave
    # a torn MIND.md
    out = "\n".join(lines) + "\n"
    tmp = root / "MIND.md.tmp"
    tmp.write_text(out, encoding="utf-8")
    os.replace(str(tmp), str(root / "MIND.md"))


def _lossy_fm(text):
    """Frontmatter constructs this parser cannot roundtrip (review m4).

    Indented non-dash lines and block scalars are silently DROPPED by
    _parse_yaml; stamping such a file back would destroy the data. -> []
    when safe to rewrite.
    """
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)",
                 text, re.S)
    if not m:
        return []
    bad = []
    for i, raw in enumerate(m.group(1).splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        s = raw.strip()
        if raw[0] in (" ", "\t"):
            if s == "-" or s.startswith("- "):
                continue      # list item
            bad.append(f"indented line {i} (continuation/block scalar)")
            continue
        if ":" not in raw:
            bad.append(f"line {i} without ':' (dropped by parser)")
            continue
        v = raw.partition(":")[2].strip()
        if re.fullmatch(r"[|>][-+]?\d*", v):
            bad.append(f"line {i} block scalar {v!r}")
        q = None
        escaped = False
        for j, ch in enumerate(v):
            if escaped:
                escaped = False
                continue
            if ch == "\\" and q == '"':
                escaped = True
                continue
            if ch in ("\"", "'"):
                q = None if q == ch else ch
                continue
            if ch == "#" and q is None and (
                    j == 0 or v[j - 1].isspace()):
                bad.append(f"line {i} inline comment")
                break
    return bad


def _run_sync(root, lenient=False, quiet=False):
    md_dir = root / "MIND"
    files = sorted(md_dir.rglob("*.md")) if md_dir.is_dir() else []
    errors, warns = [], []
    today = date.today().isoformat()
    entries = []          # (file, fm, body, rel, id)
    seen = {}             # id -> rel
    claimed = set()       # EVERY fm id seen, incl. records failing later
    pending = []          # (file, fm, body, rel) without id yet
    lossy = {}            # file -> constructs that would be dropped

    for f in files:
        rel = f.relative_to(md_dir).as_posix()
        try:
            # utf-8-sig: a BOM must not make the record unparseable
            # (audit: BOM -> record silently lost with rc=0); a broken
            # byte sequence must not lock out the whole store either.
            text = f.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"{rel}: read failed: {e}")
            continue
        lost = _lossy_fm(text)
        if lost:
            lossy[f] = lost
            errors.append(f"{rel}: unsupported frontmatter "
                          f"({'; '.join(lost)}) - NOT rewritten, "
                          "fix by hand")
        fm, body = parse_frontmatter(text)
        # reserve the id BEFORE any validation skip: a broken max-id file
        # must not lose its id to a new record (review: id theft)
        if fm.get("id") not in (None, ""):
            try:
                claimed.add(int(fm["id"]))
            except (TypeError, ValueError):
                pass  # bad id is reported below by the id check
        typ = fm.get("type")
        if typ not in TYPES:
            errors.append(f"{rel}: missing/unknown type {typ!r}")
            continue
        # stamp defaults (in memory first; written back only for kept files)
        if not fm.get("path"):
            fm["path"] = re.sub(r"-v\d+$", "", f.stem)
        if not fm.get("status"):
            fm["status"] = DEFAULT_STATUS.get(typ, "active")
        if fm.get("version") in (None, ""):
            m = re.search(r"-v(\d+)$", f.stem)
            fm["version"] = int(m.group(1)) if m else 1
        try:
            fm["version"] = int(fm["version"])
        except (TypeError, ValueError):
            errors.append(f"{rel}: bad version {fm['version']!r}")
            continue
        if not fm.get("author"):
            fm["author"] = "mind"
        if not fm.get("updated"):
            fm["updated"] = today
        fm.setdefault("importance", 3)
        fm.setdefault("confidence", 0.5)
        # normalize link fields when present: scalar -> list, str ids -> int
        if isinstance(fm.get("related_mem"), str) and fm["related_mem"].strip():
            fm["related_mem"] = _split_csv(fm["related_mem"])
        if isinstance(fm.get("related_mind"), str) and \
                fm["related_mind"].strip():
            fm["related_mind"] = _split_csv(fm["related_mind"])
        if isinstance(fm.get("related_mind"), list):
            norm = []
            for x in fm["related_mind"]:
                try:
                    norm.append(int(x))
                except (TypeError, ValueError):
                    warns.append(f"{rel}: bad related_mind id {x!r}")
            fm["related_mind"] = norm

        verrs, vwarns = validate_record(fm, rel)
        if verrs and not lenient:
            errors.extend(verrs)
            continue
        if verrs:
            warns.extend(f"(lenient) {e}" for e in verrs)
        warns.extend(vwarns)

        if fm.get("id") in (None, ""):
            pending.append((f, fm, body, rel))
            continue
        try:
            fid = int(fm["id"])
        except (TypeError, ValueError):
            errors.append(f"{rel}: bad id {fm['id']!r}")
            continue
        if fid in seen:
            errors.append(f"{rel}: duplicate id {fid} (also {seen[fid]})")
            continue
        seen[fid] = rel
        entries.append((f, fm, body, rel, fid))

    # assign ids to new/legacy files (sorted file order, max+1 over ALL
    # claimed ids AND the stored high-water mark - deleting the max-id
    # file must not recycle its id, review M3)
    max_id_floor = 0
    db0 = open_db(root)
    try:
        row = db0.execute("SELECT v FROM meta WHERE k='md_max_id'"
                          ).fetchone()
        if row:
            max_id_floor = int(row["v"])
    except (sqlite3.Error, TypeError, ValueError):
        max_id_floor = 0
    finally:
        db0.close()
    next_id = max(max_id_floor, max(claimed) if claimed else 0) + 1
    assigned = 0
    for (f, fm, body, rel) in pending:
        fid = next_id
        next_id += 1
        fm["id"] = fid
        seen[fid] = rel
        claimed.add(fid)
        entries.append((f, fm, body, rel, fid))
        assigned += 1

    # stamp frontmatter back (id/defaults/normalization)
    for (f, fm, body, rel, _fid) in entries:
        if f in lossy:
            continue          # rewriting would drop lines (see above)
        text = dump_frontmatter(fm)
        if body:
            text += body if body.endswith("\n") else body + "\n"
        try:
            if f.read_text(encoding="utf-8-sig") != text:
                write_md(f, fm, body)
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"{rel}: stamp failed: {e}")

    # supersedes -> parent_id
    by_rel = {rel: fid for (_f, _fm, _b, rel, fid) in entries}
    by_stem, by_path = {}, {}
    for (_f, fm, _b, rel, fid) in entries:
        by_stem[Path(rel).stem] = fid
        by_path.setdefault(str(fm.get("path")), fid)

    def resolve_sup(val):
        s = str(val).strip()
        if s in by_rel:
            return by_rel[s]
        stem = re.sub(r"\.md$", "", Path(s.replace("\\", "/")).name)
        if stem in by_stem:
            return by_stem[stem]
        if s in by_path:
            return by_path[s]
        return None

    # facts over the SAME file snapshot entries were built from (fresh
    # mtimes: stamping above just rewrote some of them) - a count/newest
    # taken from a later rglob could disagree with the indexed set
    newest, size = 0.0, 0
    for f in files:
        try:
            st = f.stat()
            newest = max(newest, st.st_mtime)
            size += st.st_size
        except OSError:
            pass
    count = len(files)
    md_max_id = max(max_id_floor, max(claimed) if claimed else 0)

    # full rebuild of the derived index (md is the truth)
    db = open_db(root)
    try:
        db.execute("DELETE FROM memories")
        db.execute("DELETE FROM memories_fts")
        for (f, fm, body, rel, fid) in sorted(entries, key=lambda e: e[4]):
            pid = None
            if fm.get("supersedes"):
                pid = resolve_sup(fm["supersedes"])
                if pid is None:
                    warns.append(f"{rel}: supersedes target not found: "
                                 f"{fm['supersedes']!r}")
            db.execute(
                "INSERT INTO memories (id,file,path,type,status,content,"
                "body,version,parent_id,importance,confidence,scope,"
                "author,session,created_at,updated_at,related_mind,"
                "related_mem,fm_json) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, rel, str(fm.get("path") or ""), str(fm.get("type")),
                 str(fm.get("status")), _build_search_text(fm, body),
                 body or "", fm["version"], pid,
                 int(fm.get("importance", 3)),
                 float(fm.get("confidence", 0.5)),
                 fm.get("scope"), fm.get("author") or "mind",
                 fm.get("session"), fm.get("created") or fm["updated"],
                 fm["updated"],
                 json.dumps(fm.get("related_mind") or [],
                            ensure_ascii=False),
                 json.dumps(fm.get("related_mem") or [],
                            ensure_ascii=False),
                 json.dumps(fm, ensure_ascii=False)))
            db.execute("INSERT INTO memories_fts (rowid, search_text) "
                       "VALUES (?,?)",
                       (fid, _build_search_text(fm, body)))
        # stamp source facts BEFORE commit: meta written after commit
        # used to be rolled back on close (review C1) - drift detection
        # was dead and every read command re-ran a full rebuild forever
        db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES "
                   "('md_count', ?)", (str(count),))
        db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES "
                   "('md_newest', ?)", (repr(newest or 0.0),))
        db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES "
                   "('md_size', ?)", (str(size),))
        db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES "
                   "('md_max_id', ?)", (str(md_max_id),))
        db.commit()
        write_index(db, root)
    finally:
        db.close()

    for w in warns:
        print(f"WARN: {w}", file=sys.stderr)
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if not quiet:
        print(f"synced {len(entries)} records, {assigned} ids assigned, "
              f"{len(errors)} errors")
    if errors and not lenient:
        return 1
    return 0


def cmd_sync(args):
    return _run_sync(find_root(), lenient=bool(args.lenient), quiet=False)


# ---------------------------------------------------------------------------
# record helpers
# ---------------------------------------------------------------------------

def _find_row(db, target):
    t = str(target).strip()
    m = re.fullmatch(r"(\d+)(?:@v(\d+))?", t)
    if m:
        row = db.execute("SELECT * FROM memories WHERE id=?",
                         (int(m.group(1)),)).fetchone()
        # honor an explicit version: 1@v9 must not silently return v1
        if row is not None and m.group(2) and \
                int(row["version"]) != int(m.group(2)):
            return None
        return row
    t2 = t.replace("\\", "/")
    esc = t2.replace("%", "\\%").replace("_", "\\_")  # wildcards are literal
    candidates = (
        ("SELECT * FROM memories WHERE file=?", t2),
        ("SELECT * FROM memories WHERE file=?", t2 + ".md"),
        ("SELECT * FROM memories WHERE path=?", t2),
        ("SELECT * FROM memories WHERE file LIKE ? ESCAPE '\\'",
         "%/" + esc),
        ("SELECT * FROM memories WHERE file LIKE ? ESCAPE '\\'",
         "%/" + esc + ".md"),
    )
    for sql, param in candidates:
        r = db.execute(sql, (param,)).fetchone()
        if r:
            return r
    return None


def get_record(root, target):
    """-> dict(row, fm, body, file, rel) or None. Any status."""
    ensure_index(root)
    db = open_db(root)
    try:
        row = _find_row(db, target)
    finally:
        db.close()
    if row is None:
        return None
    fpath = root / "MIND" / row["file"]
    if not fpath.is_file():
        return None
    fm, body = parse_frontmatter(fpath.read_text(encoding="utf-8-sig"))
    return {"row": row, "fm": fm, "body": body, "file": fpath,
            "rel": row["file"]}


def _num_eq(a, b):
    """Numeric equality with str fallback (1 vs 1.0, '0.40' vs 0.4).

    str() equality alone false-cleared trait notes when the same value
    was written differently (review 26.11).
    """
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def save_record(root, rec, fm):
    """Write fm back in place (no version bump) + resync."""
    # lossy guard (review note): stamping a record whose frontmatter has
    # constructs this parser cannot roundtrip would silently destroy them
    try:
        raw = rec["file"].read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raw = ""
    lossy = _lossy_fm(raw)
    if lossy:
        print(f"refusing to rewrite lossy frontmatter "
              f"({'; '.join(lossy)}): {rec['rel']}")
        return 1
    write_md(rec["file"], fm, rec["body"])
    # lenient: the mutation already validated THIS record; a foreign
    # broken file must not roll back the mutation (review: strict rc lie)
    return _run_sync(root, lenient=True, quiet=True)


def _split_csv(s):
    return [x.strip() for x in str(s).split(",") if x.strip()]


def _as_list(v):
    """Link field -> list: already a list, comma-string, or empty."""
    if isinstance(v, list):
        return list(v)
    if isinstance(v, str) and v.strip():
        return _split_csv(v)
    return []


def _split_ids(s):
    out = []
    for x in _split_csv(s):
        try:
            out.append(int(x))
        except ValueError:
            raise ValueError(f"bad related_mind id: {x!r}")
    return out


def _slug(s):
    s = str(s).strip().replace("\\", "-").replace("/", "-")
    s = re.sub(r"[^\w.\-]+", "-", s, flags=re.UNICODE)
    s = re.sub(r"-{2,}", "-", s).strip("-.")
    return s


def _collision_free(path):
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%H%M%S")
    cand = path.with_name(f"{path.stem}-{stamp}{path.suffix}")
    n = 2
    while cand.exists():
        cand = path.with_name(f"{path.stem}-{stamp}-{n}{path.suffix}")
        n += 1
    return cand


def _today():
    return date.today().isoformat()


def _plus_days(n):
    return (date.today() + timedelta(days=n)).isoformat()


def _alive_sql(extra=""):
    return ("SELECT * FROM memories "
            "WHERE status NOT IN ('superseded','deleted')" + extra)


def _citation_list(db, ids_json):
    try:
        ids = json.loads(ids_json or "[]")
    except (TypeError, ValueError):
        ids = []
    out = []
    for i in ids:
        r = db.execute("SELECT version FROM memories WHERE id=?",
                       (i,)).fetchone()
        out.append(f"{i}@v{r['version']}" if r else f"{i}@v?")
    return out


def _print_validation(errs, warns, where):
    # stderr: a WARN must never corrupt a --json stdout (review MED)
    for w in warns:
        print(f"WARN: {w}", file=sys.stderr)
    if errs:
        print(f"{where}: validation failed:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# CRUD: remember / update / forget / restore / list / show / link
# ---------------------------------------------------------------------------

def cmd_remember(args):
    if bool(args.text) == bool(args.file):
        print("remember: exactly one of TEXT or --file is required")
        return 2
    root = find_root()
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            print(f"remember: cannot read {args.file}: {e}")
            return 2
        fm, body = parse_frontmatter(raw)
    else:
        # TEXT may carry its own frontmatter (slice 1: the MCP client has
        # no temp-file etiquette - every type is structured, so bare body
        # text would always fail validation); plain text stays a body
        text = args.text or ""
        fm, body = (parse_frontmatter(text)
                    if text.lstrip().startswith("---") else ({}, text))
    typ = args.type or fm.get("type")
    if typ not in TYPES:
        print(f"remember: --type required (one of {', '.join(TYPES)})")
        return 1
    fm["type"] = typ
    if args.path:
        fm["path"] = args.path
    if not fm.get("path"):
        fm["path"] = (f"{typ}-"
                      f"{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    if args.scope:
        fm["scope"] = args.scope
    if args.importance is not None:
        fm["importance"] = args.importance
    if args.related_mem:
        fm["related_mem"] = _split_csv(args.related_mem)
    if args.related_mind:
        try:
            fm["related_mind"] = _split_ids(args.related_mind)
        except ValueError as e:
            print(f"remember: {e}")
            return 1
    if args.author:
        fm["author"] = args.author
    # creation must start at the type's entry status (review M4): a
    # --file frontmatter must not mint an idea already in testing, a
    # habit already active or a trait already active - those gates
    # belong to status/approve cmds (no self-sanction: the agent cannot
    # activate a trait about the user by itself, veto step-1 logic)
    want_st = fm.get("status")
    entry_st = DEFAULT_STATUS.get(typ, "active")
    if typ in ("idea", "habit", "trait") and want_st not in (None, "", entry_st):
        prog = {"idea": "`idea status`", "habit": "`habit approve`",
                "trait": "`trait approve`"}[typ]
        print(f"remember: new {typ} must start as {entry_st!r} "
              f"(got {want_st!r}); progress it with {prog}")
        return 1
    fm["status"] = want_st or entry_st
    fm.setdefault("version", 1)
    fm.setdefault("author", "mind")
    fm.setdefault("importance", 3)
    fm.setdefault("confidence", 0.5)
    fm["updated"] = _today()
    fm.pop("id", None)
    fm.pop("supersedes", None)

    errs, warns = validate_record(fm, "remember")
    if not _print_validation(errs, warns, "remember"):
        print("  (structured types need --file with full frontmatter)")
        return 1

    typ_dir = root / "MIND" / typ
    stem = _slug(fm["path"]) or f"{typ}-record"
    # a path must be unique among live records (review m6): two of them
    # make every path lookup return an arbitrary one
    ensure_index(root)
    db0 = open_db(root)
    try:
        dup = db0.execute(
            "SELECT id, status FROM memories WHERE path=? AND status "
            "NOT IN ('superseded','deleted')",
            (str(fm["path"]),)).fetchone()
    finally:
        db0.close()
    if dup:
        print(f"remember: path {fm['path']!r} already used by live record "
              f"{dup['id']}@... ({dup['status']}); pick another --path")
        return 1
    target = _collision_free(typ_dir / f"{stem}.md")
    write_md(target, fm, body)
    rc = _run_sync(root, lenient=True, quiet=True)
    rel = target.relative_to(root / "MIND").as_posix()
    db = open_db(root)
    row = db.execute("SELECT id, version FROM memories WHERE file=?",
                     (rel,)).fetchone()
    db.close()
    if not row:
        print(f"remember: sync did not index {rel} (rc={rc})")
        return rc or 1
    print(f"remembered {row['id']}@v{row['version']} ({rel})")
    return 0 if rc == 0 else rc


def cmd_update(args):
    if bool(args.text) == bool(args.file):
        print("update: exactly one of TEXT or --file is required")
        return 2
    root = find_root()
    rec = get_record(root, args.target)
    if not rec:
        print(f"update: not found: {args.target}")
        return 1
    # idempotent resume (review M2): a kill after the second write leaves
    # md in the final state, but get_record now reports the target as
    # superseded - that is the SUCCESS case, not an error
    if rec["row"]["status"] in DEAD:
        old_fm_0 = rec["fm"]
        typ0 = old_fm_0.get("type") or rec["row"]["type"]
        base0 = re.sub(r"-v\d+$", "", Path(rec["rel"]).stem)
        ver0 = int(old_fm_0.get("version", rec["row"]["version"]))
        child0 = root / "MIND" / str(typ0) / f"{base0}-v{ver0 + 1}.md"
        done = False
        if child0.is_file():
            try:
                cf, _ = parse_frontmatter(
                    child0.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError):
                cf = {}
            done = str(cf.get("supersedes") or "") == rec["rel"]
        if done:
            rc = _run_sync(root, lenient=True, quiet=True)
            print(f"update: resumed (already done); "
                  f"{args.target} -> superseded by {child0.name}")
            return rc
        print(f"update: record is {rec['row']['status']}, not editable")
        return 1
    old_fm = rec["fm"]
    # lossy guard (review 26.11): update stamps the record file back, so
    # constructs this parser cannot roundtrip would be destroyed here too
    try:
        raw_rec = rec["file"].read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raw_rec = ""
    lossy = _lossy_fm(raw_rec)
    if lossy:
        print(f"update: refusing to rewrite lossy frontmatter "
              f"({'; '.join(lossy)}): {rec['rel']}")
        return 1
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            print(f"update: cannot read {args.file}: {e}")
            return 2
        src_fm, new_body = parse_frontmatter(raw)
    else:
        # same as remember: TEXT with embedded frontmatter updates fields,
        # bare TEXT replaces only the body (slice 1 / MCP)
        text = args.text or ""
        src_fm, new_body = (parse_frontmatter(text)
                            if text.lstrip().startswith("---")
                            else ({}, text))
    new_fm = dict(old_fm)
    for k, v in src_fm.items():
        # operational identity: never taken from --file (mirrors mem.py
        # update: no path/type/version hijack, review P0 invariance)
        if k in ("id", "version", "supersedes", "updated", "path", "type"):
            continue
        new_fm[k] = v
    new_ver = int(old_fm.get("version", 1)) + 1
    new_fm["version"] = new_ver
    new_fm["supersedes"] = rec["rel"]
    new_fm["updated"] = _today()
    new_fm.pop("id", None)

    # status changes through update --file must obey the same gates as
    # `idea status`: transitions, criterion, WIP (review: file bypass)
    old_st = old_fm.get("status")
    new_st = new_fm.get("status")
    if new_fm.get("type") == "idea" and new_st and new_st != old_st:
        allowed = IDEA_TRANSITIONS.get(old_st, ())
        if new_st not in allowed:
            print(f"update: idea {old_st} -> {new_st} not allowed "
                  f"(allowed: {', '.join(allowed) or 'none - terminal'}); "
                  "use `idea status`/`idea kill`")
            return 1
        crit = str(new_fm.get("criterion") or "").strip()
        if new_st in ("testing", "won") and not crit:
            print("update: idea criterion required before "
                  f"{new_st}")
            return 1
        if new_st == "lost":
            print("update: use `idea kill ID --by REASON` for lost")
            return 1
        if new_st in ("drafted", "testing"):
            db = open_db(root)
            try:
                cur = _idea_wip_count(db)
            finally:
                db.close()
            add = 0 if old_st in ("drafted", "testing") else 1
            if cur + add > WIP_MAX:
                print(f"update: idea WIP full ({WIP_MAX} drafted/testing)")
                return 1

    # trait/habit status changes never ride through update --file: they
    # belong to their approve/revise cmds (same honesty rule as remember,
    # otherwise the entry gate above is decorative)
    if new_st and new_st != old_st and new_fm.get("type") in ("trait",
                                                              "habit"):
        prog = ("`trait approve`" if new_fm["type"] == "trait"
                else "`habit approve`/`habit revise`")
        print(f"update: {new_fm['type']} {old_st} -> {new_st} not allowed "
              f"through update; use {prog}")
        return 1

    # a value recalc consumes the pending notes: they were the trigger,
    # keeping them would re-fire the digest reminder forever; an explicit
    # `notes:` in --file is user input and survives (review 26.11)
    if (new_fm.get("type") == "trait"
            and not _num_eq(old_fm.get("value"), new_fm.get("value"))
            and "notes" not in src_fm):
        new_fm.pop("notes", None)

    errs, warns = validate_record(new_fm, "update")
    if not _print_validation(errs, warns, "update"):
        return 1

    base = re.sub(r"-v\d+$", "", Path(rec["rel"]).stem)
    typ = new_fm["type"]
    new_file = root / "MIND" / typ / f"{base}-v{new_ver}.md"
    if new_file.exists():
        # idempotent resume: a kill between the two writes leaves the new
        # file behind; continue when it carries exactly this update's
        # supersedes stamp, otherwise it is a foreign collision
        try:
            ofm, _ = parse_frontmatter(
                new_file.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError):
            ofm = {}
        if str(ofm.get("supersedes") or "") != rec["rel"]:
            print(f"update: target file already exists: {new_file}")
            return 1
    if new_file.exists() and str(old_fm.get("status")) == "superseded":
        # kill after the second write, before sync: md is already in the
        # final state, only the index is stale - rebuild and report done
        rc = _run_sync(root, lenient=True, quiet=True)
        print(f"update: resumed, index rebuilt (rc={rc}); "
              f"{rec['row']['id']}@v{rec['row']['version']} -> superseded")
        return rc
    write_md(new_file, new_fm, new_body)
    old_fm2 = dict(old_fm)
    old_fm2["status"] = "superseded"
    write_md(rec["file"], old_fm2, rec["body"])
    rc = _run_sync(root, lenient=True, quiet=True)
    rel_new = new_file.relative_to(root / "MIND").as_posix()
    db = open_db(root)
    row = db.execute("SELECT id, version FROM memories WHERE file=?",
                     (rel_new,)).fetchone()
    db.close()
    if not row:
        print(f"update: sync did not index {rel_new} (rc={rc})")
        return rc or 1
    print(f"updated {row['id']}@v{row['version']} ({rel_new}); "
          f"{rec['row']['id']}@v{rec['row']['version']} -> superseded")
    return 0 if rc == 0 else rc


def cmd_forget(args):
    root = find_root()
    rec = get_record(root, args.target)
    if not rec:
        print(f"forget: not found: {args.target}")
        return 1
    if rec["row"]["status"] == "deleted":
        print(f"forget: already deleted: {args.target}")
        return 1
    fm = dict(rec["fm"])
    fm["predelete_status"] = fm.get("status")
    fm["status"] = "deleted"
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"forgot {rec['row']['id']}@v{rec['row']['version']} "
              f"(was {fm['predelete_status']})")
    return rc


def cmd_restore(args):
    root = find_root()
    rec = get_record(root, args.target)
    if not rec:
        print(f"restore: not found: {args.target}")
        return 1
    if rec["row"]["status"] != "deleted":
        print(f"restore: not deleted (status={rec['row']['status']})")
        return 1
    fm = dict(rec["fm"])
    prev = fm.pop("predelete_status", None)
    typ = fm.get("type")
    matrix = STATUS_MATRIX.get(typ, ("active",))
    if prev not in matrix or prev == "deleted":
        prev = "active" if "active" in matrix else matrix[0]
    fm["status"] = prev
    if prev in DEAD:
        # not a resurrection (review m1): a record restored back to
        # superseded cannot collide with its child - no gate needed
        rc = save_record(root, rec, fm)
        if rc == 0:
            print(f"restored {rec['row']['id']}@v{rec['row']['version']} "
                  f"-> {prev}")
        return rc
    db = open_db(root)
    child = db.execute(
        "SELECT id, status FROM memories WHERE parent_id=? AND "
        "status NOT IN ('superseded','deleted')",
        (rec["row"]["id"],)).fetchone()
    db.close()
    if child:
        print(f"restore: refused, active child {child['id']} "
              f"({child['status']}) exists")
        return 1
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"restored {rec['row']['id']}@v{rec['row']['version']} "
              f"-> {prev}")
    return rc


def cmd_list(args):
    root = find_root()
    ensure_index(root)
    db = open_db(root)
    try:
        sql, params = "SELECT * FROM memories WHERE 1=1", []
        st = args.status
        if st and st != "all":
            sql += " AND status=?"
            params.append(st)
        elif not st:
            sql += " AND status NOT IN ('superseded','deleted')"
        if args.type:
            sql += " AND type=?"
            params.append(args.type)
        sql += " ORDER BY updated_at DESC, id DESC"
        rows = list(db.execute(sql, params))
        if args.json:
            out = []
            for r in rows:
                out.append({"id": r["id"],
                            "at": f"{r['id']}@v{r['version']}",
                            "status": r["status"], "type": r["type"],
                            "path": r["path"], "file": r["file"],
                            "updated": r["updated_at"],
                            "importance": r["importance"],
                            "confidence": r["confidence"],
                            "scope": r["scope"]})
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print(f"{r['id']:>4} v{r['version']} {r['status']:<11} "
                      f"{r['type']:<8} {r['updated_at']} imp{r['importance']} "
                      f"{r['path']}  <- {r['file']}")
            if not rows:
                print("(no records)")
        return 0
    finally:
        db.close()


def cmd_show(args):
    root = find_root()
    rec = get_record(root, args.target)
    if not rec:
        print(f"show: not found: {args.target}")
        return 1
    r, fm = rec["row"], rec["fm"]
    print(f"{r['id']}@v{r['version']} [{r['status']}] {r['type']} "
          f"{r['path']} ({r['updated_at']}, imp {r['importance']}, "
          f"conf {r['confidence']}, scope {r['scope']}, "
          f"author {r['author']})")
    print(f"file: MIND/{r['file']}")
    if r["parent_id"]:
        print(f"supersedes id: {r['parent_id']}")
    db = open_db(root)
    try:
        out = _citation_list(db, r["related_mind"])
        out_ids = _as_list(fm.get("related_mind"))
        inc = []
        if out_ids:
            marks = ",".join("?" * len(out_ids))
            for x in db.execute(
                    f"SELECT id, version FROM memories WHERE id IN "
                    f"({marks})", out_ids):
                inc.append(f"{x['id']}@v{x['version']}")
        if out:
            print(f"related_mind -> {', '.join(out)}")
        if inc:
            print(f"related_mind <- {', '.join(inc)}")
        rm = _as_list(fm.get("related_mem"))
        if rm:
            print(f"related_mem -> {', '.join(map(str, rm))}")
    finally:
        db.close()
    print()
    for k, v in fm.items():
        print(f"{k}: {_fmt(v)}")
    print()
    print((r["body"] or "").strip())
    return 0


def cmd_link(args):
    root = find_root()
    rec = get_record(root, args.target)
    if not rec:
        print(f"link: not found: {args.target}")
        return 1
    add = list(args.ids or [])
    off = list(args.off or [])
    if not add and not off:
        db = open_db(root)
        try:
            out = _citation_list(db, rec["row"]["related_mind"])
        finally:
            db.close()
        rm = _as_list(rec["fm"].get("related_mem"))
        print(f"related_mind -> {', '.join(out) if out else '(none)'}")
        print(f"related_mem  -> {', '.join(map(str, rm)) if rm else '(none)'}")
        return 0
    ensure_index(root)
    db = open_db(root)
    try:
        cur = [int(x) for x in _as_list(rec["fm"].get("related_mind"))]
        for x in add:
            try:
                i = int(x)
            except ValueError:
                print(f"link: bad id {x!r}")
                return 1
            if not db.execute("SELECT 1 FROM memories WHERE id=?",
                              (i,)).fetchone():
                print(f"link: no such record: {i}")
                return 1
            if i not in cur:
                cur.append(i)
        for x in off:
            try:
                i = int(x)
            except ValueError:
                print(f"link: bad id {x!r}")
                return 1
            while i in cur:
                cur.remove(i)
    finally:
        db.close()
    fm = dict(rec["fm"])
    if cur:
        fm["related_mind"] = cur
    else:
        fm.pop("related_mind", None)
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"link {rec['row']['id']}: related_mind = "
              f"{cur if cur else '(none)'}")
    return rc


# ---------------------------------------------------------------------------
# embed + hybrid recall (slice 1.5, DESIGN 13): remote-only, own mind_vec
# ---------------------------------------------------------------------------

def _embed_url():
    # read at call time so tests (and a moved forward) can override env
    return os.environ.get("MIND_EMBED_URL", EMBED_URL_DEFAULT)


def _embed_model():
    return os.environ.get("MIND_EMBED_MODEL", EMBED_MODEL_DEFAULT)


def _vec_sig(text):
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


def embed_budget(texts):
    """Seconds to wait for one /v1/embeddings response (mem formula):
    the phone embeds ~100 chars/s (measured 327 tok = 10-16 s); 2.5x
    margin (total//40), floor 60 s, cap 600 s - a dead peer stalls a
    small run for ~1 min, not ten."""
    total = sum(len(t) for t in texts)
    return min(600, max(EMBED_TIMEOUT_S, total // 40 + EMBED_TIMEOUT_S))


# corporate-proxy audit (mem 16.11): local embed must bypass http_proxy
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def embed_texts(texts):
    """POST /v1/embeddings to the remote server, index-ordered vectors.

    Raises URLError/OSError/RuntimeError/ValueError on any failure: the
    caller keeps already-written batches and reports rc=1 - nothing is
    lost, the next run resumes by sig."""
    payload = json.dumps({"model": _embed_model(),
                          "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        _embed_url(), data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with _opener.open(req, timeout=embed_budget(texts)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # surface the server's own reason instead of a bare URL (mem audit)
        body = e.read()[:300].decode("utf-8", errors="replace")
        raise RuntimeError(f"embed HTTP {e.code}: {body}") from e
    items = sorted(data.get("data", []), key=lambda d: d.get("index", 0))
    vecs = [it.get("embedding") for it in items]
    if len(vecs) != len(texts) or any(v is None for v in vecs):
        raise RuntimeError(f"bad embeddings response: {len(vecs)}/{len(texts)}")
    for v in vecs:
        if any(not math.isfinite(x) for x in v):
            raise RuntimeError("embeddings response contains NaN/inf")
    return vecs


def pack_vec(v):
    return struct.pack(f"<{len(v)}f", *v)


def unpack_vec(b):
    return struct.unpack(f"<{len(b) // 4}f", b)


def cosine(a, b):
    if not a or len(a) != len(b):
        # dim mismatch (model swapped under the same name): never compare
        # partially - zip() would truncate to the shorter one (mem audit)
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def cmd_embed(args):
    """Fill mind_vec for records whose search_text/model changed (1.5).

    Increment by sig = sha1(search_text) + model: a rerun embeds only what
    is missing. Remote-only (phone via adb forward): a down server is
    rc=1 with already-written batches kept - the next run resumes.
    """
    root = find_root()
    ensure_index(root)
    model = _embed_model()
    db = open_db(root)
    done = 0
    todo = []
    try:
        # sync rebuilds memories but keeps mind_vec: drop orphans and
        # vectors of superseded versions first (update mints a new id)
        db.execute("DELETE FROM mind_vec WHERE mem_id NOT IN "
                   "(SELECT id FROM memories)")
        db.execute("DELETE FROM mind_vec WHERE mem_id IN "
                   "(SELECT id FROM memories WHERE status IN "
                   "('superseded','deleted'))")
        stored = {r["mem_id"]: (r["sig"], r["model"]) for r in
                  db.execute("SELECT mem_id, sig, model FROM mind_vec")}
        for r in db.execute(
                "SELECT id, content FROM memories "
                "WHERE status NOT IN ('superseded','deleted')"):
            text = r["content"] or ""   # sync stores search_text as content
            sig = _vec_sig(text)
            if stored.get(r["id"]) != (sig, model):
                todo.append((r["id"], sig, text))
        if not todo:
            db.commit()
            print(f"embed: 0 pending ({len(stored)} vectors current, "
                  f"model {model})")
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for i in range(0, len(todo), EMBED_BATCH):
            batch = todo[i:i + EMBED_BATCH]
            vecs = embed_texts([t for _, _, t in batch])
            for (rid, sig, _), v in zip(batch, vecs):
                db.execute(
                    "INSERT INTO mind_vec (mem_id, sig, dim, model, vec, "
                    "updated) VALUES (?,?,?,?,?,?) ON CONFLICT(mem_id) "
                    "DO UPDATE SET sig=excluded.sig, dim=excluded.dim, "
                    "model=excluded.model, vec=excluded.vec, "
                    "updated=excluded.updated",
                    (rid, sig, len(v), model, pack_vec(v), now))
                done += 1
            db.commit()
    except (urllib.error.URLError, OSError, RuntimeError, ValueError) as e:
        db.commit()   # keep whatever the completed batches wrote
        print(f"embed: remote unavailable after {done} written "
              f"({len(todo) - done} left): {e}")
        return 1
    finally:
        db.close()
    print(f"embed: {done} vectors written ({len(todo)} pending, "
          f"model {model})")
    return 0


# ---------------------------------------------------------------------------
# recall (v0 = FTS; hybrid opt-in via MIND_RECALL=hybrid, slice 1.5)
# ---------------------------------------------------------------------------

def _preview(r):
    body = (r["body"] or "").strip()
    if body:
        return body
    try:
        fm = json.loads(r["fm_json"] or "{}")
    except (TypeError, ValueError):
        fm = {}
    bits = []
    for k in PREVIEW_FIELDS.get(r["type"], ("path",)):
        v = fm.get(k)
        if v is not None and str(v).strip():
            bits.append(f"{k}={v}")
    return " | ".join(bits) if bits else r["path"]


def cmd_recall(args):
    root = find_root()
    ensure_index(root)
    terms = [t for t in re.findall(r"\w+", args.query or "", re.UNICODE)]
    if not terms:
        print("recall: empty query")
        return 1
    match = " OR ".join(t + "*" for t in terms)
    db = open_db(root)
    try:
        try:
            rows = list(db.execute(
                "SELECT m.*, bm25(memories_fts) AS b FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND "
                "m.status NOT IN ('superseded','deleted')", (match,)))
        except sqlite3.OperationalError as e:
            print(f"recall: fts error: {e}")
            return 1
        if args.type:
            rows = [r for r in rows if r["type"] == args.type]
        fts_ids = {r["id"] for r in rows}

        # --- hybrid half (slice 1.5): cosine over mind_vec, opt-in via
        # MIND_RECALL=hybrid. Default stays pure FTS until the A/B
        # usefulness gate (DESIGN 13) passes on ~50 records.
        mode = "fts (vectors: n/a)"
        cos_by_id = {}
        if os.environ.get("MIND_RECALL", "fts").lower() == "hybrid":
            vec_rows = list(db.execute(
                "SELECT v.mem_id, v.vec FROM mind_vec v "
                "JOIN memories m ON m.id = v.mem_id "
                "WHERE m.status NOT IN ('superseded','deleted')"))
            if not vec_rows:
                # embed never ran: no phone call at all (cheap honest FTS)
                mode = "fts (no vectors yet)"
            else:
                try:
                    qvec = embed_texts([args.query])[0]
                except (urllib.error.URLError, OSError, RuntimeError,
                        ValueError):
                    # dead phone must not stall the search (mem rule)
                    qvec = None
                    mode = "fts (remote server down)"
                if qvec is not None:
                    have = {r["id"] for r in rows}
                    for vr in vec_rows:
                        c = cosine(qvec, unpack_vec(vr["vec"]))
                        if vr["mem_id"] in have:
                            cos_by_id[vr["mem_id"]] = c
                        elif c >= HYBRID_MIN_COS:
                            # vector-only candidate: FTS missed it
                            m = db.execute(
                                "SELECT * FROM memories WHERE id=?",
                                (vr["mem_id"],)).fetchone()
                            if m and (not args.type
                                      or m["type"] == args.type):
                                rows.append(m)
                                cos_by_id[m["id"]] = c
                    withv = sum(1 for r in rows if r["id"] in cos_by_id)
                    mode = ("hybrid" if withv == len(rows)
                            else f"hybrid partial {withv}/{len(rows)}")
        if not rows:
            print("no hits")
            return 1
        # bm25 min-max over FTS hits ONLY: a vector-only candidate has no
        # bm25 evidence and must not stretch the normalization range
        fts_bm = [-float(r["b"] or 0.0) for r in rows
                  if r["id"] in fts_ids]
        lo, hi = (min(fts_bm), max(fts_bm)) if fts_bm else (0.0, 0.0)
        bm = [-float(r["b"] or 0.0) if r["id"] in fts_ids else 0.0
              for r in rows]
        today = date.today()
        scored = []
        for r, b in zip(rows, bm):
            if r["id"] in fts_ids:
                bn = 1.0 if hi == lo else (b - lo) / (hi - lo)
            else:
                bn = 0.0   # vector-only candidate: no bm25 evidence
            try:
                d = date.fromisoformat(str(r["updated_at"])[:10])
                age = max(0.0, (today - d).days)
            except ValueError:
                age = 365.0
            rn = 0.5 ** (age / RECENCY_DAYS)
            try:
                imp = int(r["importance"])
            except (TypeError, ValueError):
                imp = 3
            inn = min(5, max(1, imp)) / 5.0
            if mode.startswith("hybrid"):
                c = min(1.0, max(0.0, cos_by_id.get(r["id"], 0.0)))
                score = (W_HYB_BM25 * bn + W_HYB_REC * rn
                         + W_HYB_IMP * inn + W_HYB_COS * c)
            else:
                score = W_BM25 * bn + W_REC * rn + W_IMP * inn
            scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        limit = RECALL_LIMIT if args.limit is None else max(0, args.limit)
        top = scored[:limit]
        budget = RECALL_BUDGET
        if args.json:
            out = []
            for score, r in top:
                body = r["body"] or ""
                enc = body.encode("utf-8")
                if budget <= 0:
                    body = ""
                elif len(enc) > budget:
                    body = enc[:budget].decode("utf-8", errors="ignore") + \
                        " ...[truncated]"
                    budget = 0
                else:
                    budget -= len(enc)
                out.append({"id": r["id"], "at": f"{r['id']}@v{r['version']}",
                            "score": round(score, 4), "type": r["type"],
                            "path": r["path"], "file": r["file"],
                            "updated": r["updated_at"],
                            "importance": r["importance"],
                            "related_mind": json.loads(
                                r["related_mind"] or "[]"),
                            "related_mem": json.loads(r["related_mem"]
                                                      or "[]"),
                            "content": body})
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        shown = 0
        for score, r in top:
            prev = _preview(r)
            enc = prev.encode("utf-8")
            if budget <= 0:
                print(f"\n[{score:.3f}] {r['id']}@v{r['version']} "
                      f"{r['type']} {r['path']} ({r['updated_at']}, "
                      f"imp {r['importance']}) [budget exhausted]")
                shown += 1
                continue
            if len(enc) > budget:
                prev = enc[:budget].decode("utf-8",
                                           errors="ignore") + " ...[truncated]"
                budget = 0
            else:
                budget -= len(enc)
            print(f"\n[{score:.3f}] {r['id']}@v{r['version']} {r['type']} "
                  f"{r['path']} ({r['updated_at']}, imp {r['importance']})")
            print(prev)
            out_ids = json.loads(r["related_mind"] or "[]")
            rm = json.loads(r["related_mem"] or "[]")
            if out_ids:
                print(f"[related_mind -> "
                      f"{', '.join(_citation_list(db, r['related_mind']))}]")
            if rm:
                print(f"[related_mem -> {', '.join(map(str, rm))}]")
            shown += 1
        print(f"\n-- {shown}/{len(scored)} hits, budget left {budget} B, "
              f"mode: {mode}")
        return 0 if shown else 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# idea lifecycle (gates executed by code)
# ---------------------------------------------------------------------------

def _idea_wip_count(db, exclude_id=None):
    sql = ("SELECT COUNT(*) AS n FROM memories WHERE type='idea' AND "
           "status IN ('drafted','testing') AND "
           "status NOT IN ('superseded','deleted')")
    params = ()
    if exclude_id is not None:
        sql += " AND id != ?"
        params = (exclude_id,)
    return int(db.execute(sql, params).fetchone()["n"])


def _require_type(root, target, typ, alive_only=True):
    rec = get_record(root, target)
    if not rec:
        print(f"not found: {target}")
        return None, 1
    if rec["row"]["type"] != typ:
        print(f"expected type {typ}, got {rec['row']['type']}: {target}")
        return None, 1
    if alive_only and rec["row"]["status"] in DEAD:
        print(f"record is {rec['row']['status']}: {target}")
        return None, 1
    return rec, 0


def cmd_idea_new(args):
    if bool(args.hypothesis) == bool(args.file):
        print("idea new: exactly one of --hypothesis or --file is required")
        return 2
    root = find_root()
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            print(f"idea new: cannot read {args.file}: {e}")
            return 2
        fm, body = parse_frontmatter(raw)
    else:
        fm, body = {}, ""
    fm["type"] = "idea"
    if args.path:
        fm["path"] = args.path
    if not fm.get("path"):
        print("idea new: --path required")
        return 1
    if args.hypothesis:
        fm["hypothesis"] = args.hypothesis
    if args.expected:
        fm["expected"] = args.expected
    if args.criterion:
        fm["criterion"] = args.criterion
    if args.cost:
        fm["cost"] = args.cost
    if args.related_mem:
        fm["related_mem"] = _split_csv(args.related_mem)
    fm.setdefault("scope", "project")
    # creation is always a spark (review M4): --file must not mint an
    # idea already drafted/testing and skip the WIP gate
    want_st = fm.get("status")
    if want_st not in (None, "", "spark"):
        print(f"idea new: a new idea must start as 'spark' "
              f"(got {want_st!r}); progress it with `idea status`")
        return 1
    fm["status"] = "spark"
    fm.setdefault("version", 1)
    fm.setdefault("author", "mind")
    fm.setdefault("importance", 3)
    fm.setdefault("confidence", 0.5)
    fm["updated"] = _today()
    fm.pop("id", None)
    fm.pop("supersedes", None)

    errs, warns = validate_record(fm, "idea new")
    if not _print_validation(errs, warns, "idea new"):
        return 1
    stem = _slug(fm["path"])
    target = _collision_free(root / "MIND" / "idea" / f"{stem}.md")
    write_md(target, fm, body)
    rc = _run_sync(root, lenient=True, quiet=True)
    rel = target.relative_to(root / "MIND").as_posix()
    db = open_db(root)
    row = db.execute("SELECT id, version FROM memories WHERE file=?",
                     (rel,)).fetchone()
    db.close()
    if not row:
        print(f"idea new: sync did not index {rel} (rc={rc})")
        return rc or 1
    print(f"idea {row['id']}@v{row['version']} spark ({rel})")
    return 0 if rc == 0 else rc


def cmd_idea_status(args):
    root = find_root()
    rec, rc = _require_type(root, args.target, "idea")
    if rec is None:
        return rc
    fm = dict(rec["fm"])
    old = fm.get("status")
    new = args.status
    allowed = IDEA_TRANSITIONS.get(old, ())
    if new not in allowed:
        print(f"idea: {old} -> {new} not allowed "
              f"(allowed: {', '.join(allowed) or 'none - terminal'})")
        return 1
    crit = str(fm.get("criterion") or "").strip()
    if new in ("testing", "won") and not crit:
        print("idea: criterion required before "
              f"{new} (set it via update --file)")
        return 1
    if new == "lost":
        print("idea: use `idea kill ID --by REASON` for lost")
        return 1
    if new in ("drafted", "testing"):
        db = open_db(root)
        try:
            cur = _idea_wip_count(db)
        finally:
            db.close()
        add = 0 if old in ("drafted", "testing") else 1
        if cur + add > WIP_MAX:
            print(f"idea: WIP full ({WIP_MAX} drafted/testing); "
                  "park/win/kill something first")
            return 1
    fm["status"] = new
    fm["updated"] = _today()
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"idea {rec['row']['id']}: {old} -> {new}")
    return rc


def cmd_idea_kill(args):
    root = find_root()
    rec, rc = _require_type(root, args.target, "idea")
    if rec is None:
        return rc
    fm = dict(rec["fm"])
    if fm.get("status") != "testing":
        print(f"idea kill: only a testing idea can be killed "
              f"(status={fm.get('status')}); park it or test it first")
        return 1
    if not str(args.by or "").strip():
        print("idea kill: --by REASON is required and must be non-empty")
        return 2
    fm["status"] = "lost"
    fm["killed_by"] = args.by
    fm["killed_at"] = _today()
    fm["updated"] = _today()
    # a mutated record must validate BEFORE it reaches md (review m7):
    # lenient sync after the write always returns rc=0
    errs, warns = validate_record(fm, "idea kill")
    if not _print_validation(errs, warns, "idea kill"):
        return 1
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"idea {rec['row']['id']}: testing -> lost "
              f"(killed_by: {args.by})")
    return rc


# ---------------------------------------------------------------------------
# trait lifecycle
# ---------------------------------------------------------------------------

def cmd_trait_approve(args):
    """candidate -> active, approved_by=user (mirrors habit approve).

    The entry gate in cmd_remember keeps agent-minted traits at
    candidate; only the user's word promotes them into profile.
    """
    root = find_root()
    rec, rc = _require_type(root, args.target, "trait")
    if rec is None:
        return rc
    fm = dict(rec["fm"])
    if fm.get("status") != "candidate":
        print(f"trait approve: not a candidate (status={fm.get('status')})")
        return 1
    fm["status"] = "active"
    fm["approved_by"] = "user"
    fm["approved_at"] = _today()
    fm["updated"] = _today()
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"trait {rec['row']['id']}: candidate -> active")
    return rc


def cmd_note(args):
    """Log an observation against a trait (in-place, no version bump).

    The recalc rule in docs/calibration.md needs facts to act on; `note`
    is the one-command entry point for them. side=up confirms direction
    (value should rise), side=down argues against it; 2 same-side notes
    make `digest` remind about a +-0.1 recalc. Notes are consumed by the
    next value change (cmd_update clears them).
    """
    if bool(args.text) == bool(args.file):
        print("note: exactly one of TEXT or --file is required")
        return 2
    root = find_root()
    rec, rc = _require_type(root, args.target, "trait")
    if rec is None:
        return rc
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError) as e:
            print(f"note: cannot read {args.file}: {e}")
            return 2
    else:
        text = (args.text or "").strip()
    if not text:
        print("note: empty text")
        return 2
    if "\n" in text:
        # _fmt flattens newlines (single-line YAML) - say so, don't mangle silently
        print("note: WARN multi-line input flattened to one line",
              file=sys.stderr)
    fm = dict(rec["fm"])
    notes = fm.get("notes")
    if not isinstance(notes, list):
        notes = []
    entry = {"date": _today()}
    if args.side:
        entry["side"] = args.side
    entry["text"] = text
    notes.append(entry)
    fm["notes"] = notes
    rc = save_record(root, rec, fm)
    if rc == 0:
        side = args.side or "none"
        print(f"note {rec['row']['id']}@v{rec['row']['version']} added "
              f"(side: {side}, {len(notes)} total)")
    return rc


# ---------------------------------------------------------------------------
# habit lifecycle
# ---------------------------------------------------------------------------

def cmd_habit_approve(args):
    root = find_root()
    rec, rc = _require_type(root, args.target, "habit")
    if rec is None:
        return rc
    fm = dict(rec["fm"])
    if fm.get("status") != "candidate":
        print(f"habit approve: not a candidate (status={fm.get('status')})")
        return 1
    fm["status"] = "active"
    fm["approved_by"] = "user"
    fm["approved_at"] = _today()
    if not fm.get("expires"):
        fm["expires"] = _plus_days(HABIT_EXPIRE_DAYS)
    fm["updated"] = _today()
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"habit {rec['row']['id']}: candidate -> active "
              f"(expires {fm['expires']})")
    return rc


def cmd_habit_exception(args):
    root = find_root()
    rec, rc = _require_type(root, args.target, "habit")
    if rec is None:
        return rc
    fm = dict(rec["fm"])
    if fm.get("status") != "active":
        print(f"habit exception: only an active habit "
              f"(status={fm.get('status')})")
        return 1
    exc = fm.get("exceptions")
    if not isinstance(exc, list):
        exc = []
    exc.append({"date": _today(), "why": args.why})
    fm["exceptions"] = exc
    rc = save_record(root, rec, fm)
    if rc == 0:
        flag = " [REVISION FLAG]" if len(exc) >= HABIT_EXCEPTION_LIMIT else ""
        print(f"habit {rec['row']['id']}: exception logged "
              f"({len(exc)} total){flag}")
    return rc


def cmd_habit_revise(args):
    if bool(args.keep) == bool(args.deprecate):
        print("habit revise: exactly one of --keep or --deprecate")
        return 2
    root = find_root()
    rec, rc = _require_type(root, args.target, "habit")
    if rec is None:
        return rc
    fm = dict(rec["fm"])
    if args.keep:
        fm["expires"] = _plus_days(HABIT_EXPIRE_DAYS)
        fm["updated"] = _today()
        msg = f"habit {rec['row']['id']}: revised, expires {fm['expires']}"
    else:
        fm["status"] = "deprecated"
        fm["updated"] = _today()
        msg = f"habit {rec['row']['id']}: active -> deprecated"
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(msg)
    return rc


# ---------------------------------------------------------------------------
# profile / digest
# ---------------------------------------------------------------------------

def _alive_rows(db, typ, scope=None, status=None, order=True,
                limit=None):
    sql = "SELECT * FROM memories WHERE type=? AND " \
          "status NOT IN ('superseded','deleted')"
    params = [typ]
    if status:
        sql += " AND status=?"
        params.append(status)
    if scope:
        sql += " AND scope=?"
        params.append(scope)
    if order:
        sql += " ORDER BY importance DESC, updated_at DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return list(db.execute(sql, params))


def cmd_profile(args):
    root = find_root()
    ensure_index(root)
    scope = getattr(args, "scope", None)
    db = open_db(root)
    try:
        traits = _alive_rows(db, "trait", scope=scope, status="active")
        habits = _alive_rows(db, "habit", scope=scope, status="active")
        insights = _alive_rows(db, "insight", scope=scope, status="active",
                               limit=3)
        settings = _alive_rows(db, "setting")
        counts = {r["status"]: r["n"] for r in db.execute(
            "SELECT status, COUNT(*) AS n FROM memories WHERE type='idea' "
            "AND status NOT IN ('superseded','deleted') GROUP BY status")}
        parked = [r for r in _alive_rows(db, "idea", status="parked",
                                         limit=3)]
    finally:
        db.close()
    prof = {
        "date": _today(),
        "scope": scope,
        "traits": [{"id": r["id"], "path": r["path"],
                    "value": json.loads(r["fm_json"]).get("value"),
                    "direction": json.loads(r["fm_json"]).get("direction"),
                    "confidence": r["confidence"]} for r in traits],
        "habits": [{"id": r["id"], "path": r["path"],
                    "trigger": json.loads(r["fm_json"]).get("trigger"),
                    "action": json.loads(r["fm_json"]).get("action")}
                   for r in habits],
        "insights": [{"id": r["id"], "path": r["path"],
                      "summary": json.loads(r["fm_json"]).get("summary")}
                     for r in insights],
        "idea_counts": counts,
        "parked_ideas": [{"id": r["id"], "path": r["path"]} for r in parked],
        "settings": [{"id": r["id"], "path": r["path"],
                      "mode": json.loads(r["fm_json"]).get("mode"),
                      "veto_executor": json.loads(r["fm_json"]).get(
                          "veto_executor") or "none",
                      "block_conditions": json.loads(r["fm_json"]).get(
                          "block_conditions") or []}
                     for r in settings],
    }
    if args.json:
        print(json.dumps(prof, ensure_ascii=False, indent=2))
        return 0
    print(f"MIND profile ({prof['date']}"
          + (f", scope={scope}" if scope else "") + ")")
    print("traits:")
    if prof["traits"]:
        for t in prof["traits"]:
            print(f"  {t['path']}={t['value']} conf{t['confidence']} - "
                  f"{t['direction']}")
    else:
        print("  (none active)")
    print("habits:")
    if prof["habits"]:
        for h in prof["habits"]:
            print(f"  {h['path']}: {h['trigger']} -> {h['action']}")
    else:
        print("  (none active)")
    print("insights:")
    if prof["insights"]:
        for i in prof["insights"]:
            print(f"  {i['path']}: {i['summary']}")
    else:
        print("  (none)")
    idea_bits = [f"{k} {v}" for k, v in sorted(counts.items())]
    print("ideas: " + (", ".join(idea_bits) if idea_bits else "none"))
    if prof["settings"]:
        for s in prof["settings"]:
            # honest label: who actually enforces this mode (user 26.11)
            if s["mode"] == "off":
                how = "no advice, no veto"
            elif s["mode"] == "hint":
                how = ("advisory: the agent advises, cannot enforce; "
                       f"hard-veto list NOT enforced (executor: "
                       f"{s['veto_executor']})")
            else:
                how = f"enforced by {s['veto_executor']}"
            print(f"settings: {s['path']}={s['mode']} ({how})")
            for c in s["block_conditions"]:
                print(f"  - hard-veto condition: {c}")
    return 0


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
    for s in settings:
        if s["mode"] == "off":
            continue
        for rule in s["rules"]:
            if not rule["pattern"]:
                continue
            try:
                rx = re.compile(rule["pattern"], re.IGNORECASE)
            except re.error:
                continue          # validate_record rejects, belt+braces
            if args.list or rx.search(action or ""):
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
    for key in ("mem_py", "mem_root"):
        # a present-but-incomplete contract must not raise Path(None)
        # out of _mem_contract as a bare traceback (review m8)
        if not contract.get(key):
            out["detail"] = f"CONTRACT.json missing {key}"
            return out
    try:
        mod = load_bridge()
    except Exception as e:
        out["detail"] = f"bridge load failed: {e}"
        return out
    br = mod.MemBridge(contract.get("mem_py"),
                       contract.get("mem_root"), timeout=timeout)
    try:
        br.timeout = min(br.timeout, left())
        br.start()
        br.handshake()
        # mem_root sanity: the child prints "mem serve: root=..." to
        # stderr; a mismatch means it ignored MEM_ROOT and serves the
        # wrong memory (review MED)
        want_root = str(Path(contract.get("mem_root") or "")).rstrip("/\\")
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


# ---------------------------------------------------------------------------
# doctor mem
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# MCP serve (slice 1): newline-delimited JSON-RPC over stdio, the same
# shape as mem.py serve - opencode plugs it into opencode.jsonc
# ---------------------------------------------------------------------------

MCP_SERVER_INFO = {"name": "mind", "version": "0.2"}
MCP_PROTOCOL_DEFAULT = "2024-11-05"
MCP_INSTRUCTIONS = (
    "MIND cognitive layer: traits, habits, ideas, insights. md files are "
    "the source of truth, mind.db is derived. Honesty gates are enforced "
    "by the code: remember only creates candidates (approve is user-only), "
    "a trait value change consumes its pending notes (recalc rule in "
    "docs/calibration.md). mem itself is read-only through its own MCP.")

# everything the CLI defines; filled below the cmd_* definitions
MUTATING = {cmd_remember, cmd_update, cmd_forget, cmd_restore, cmd_sync,
            cmd_link, cmd_idea_new, cmd_idea_status, cmd_idea_kill,
            cmd_habit_approve, cmd_habit_exception, cmd_habit_revise,
            cmd_trait_approve, cmd_note, cmd_embed}


def _mcp_tools():
    type_prop = {"type": "string", "enum": list(TYPES)}
    target_prop = {"type": "string",
                   "description": "id, id@vN or path (citation rules)"}
    return [
        {"name": "recall",
         "description": "Search MIND records (FTS; hybrid when "
                        "MIND_RECALL=hybrid). Footer names the mode.",
         "inputSchema": {"type": "object", "properties": {
             "query": {"type": "string"},
             "limit": {"type": "integer"},
             "type": dict(type_prop),
         }, "required": ["query"]}},
        {"name": "profile",
         "description": "Compact self-block: active traits by scope, "
                        "active habits, recent insights, veto policy.",
         "inputSchema": {"type": "object", "properties": {
             "scope": {"type": "string",
                       "enum": ["agent", "project", "companion"]},
         }}},
        {"name": "digest",
         "description": "Everything pending in one list: approval "
                        "candidates, recalc_pending traits, ideas, habits, "
                        "mem contract.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "show",
         "description": "Show one record in full (frontmatter, links, "
                        "body).",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
         }, "required": ["target"]}},
        {"name": "list",
         "description": "List records with type/status filters.",
         "inputSchema": {"type": "object", "properties": {
             "type": dict(type_prop),
             "status": {"type": "string"},
         }}},
        {"name": "note",
         "description": "Log an observation on a trait (in-place, no "
                        "version bump). side=up confirms direction, "
                        "side=down argues against; 2 same-side notes make "
                        "digest remind about a value recalc.",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
             "text": {"type": "string"},
             "side": {"type": "string", "enum": ["up", "down"]},
         }, "required": ["target", "text"]}},
        {"name": "remember",
         "description": "Create a record. Trait/habit/idea are created as "
                        "CANDIDATES only: activating them needs the user "
                        "(`trait approve`/`habit approve`).",
         "inputSchema": {"type": "object", "properties": {
             "text": {"type": "string", "description": "record body"},
             "type": dict(type_prop),
             "path": {"type": "string"},
             "scope": {"type": "string"},
             "importance": {"type": "integer", "minimum": 1, "maximum": 5},
             "related_mem": {"type": "string"},
         }, "required": ["text"]}},
        {"name": "update",
         "description": "New version of a record. Changing a trait's "
                        "value = recalc per the calibration rule and "
                        "consumes its pending notes.",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
             "text": {"type": "string", "description": "frontmatter+body"},
         }, "required": ["target", "text"]}},
        {"name": "forget",
         "description": "Tombstone a record (status=deleted).",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
         }, "required": ["target"]}},
        {"name": "restore",
         "description": "Restore a tombstoned record back to active.",
         "inputSchema": {"type": "object", "properties": {
             "target": dict(target_prop),
         }, "required": ["target"]}},
        {"name": "veto",
         "description": "Check an action against hard-veto rules, or list "
                        "them. rc=1 = vetoed (the action must not run).",
         "inputSchema": {"type": "object", "properties": {
             "action": {"type": "string",
                        "description": "the command/description to check"},
             "list": {"type": "boolean", "default": False},
         }}},
        {"name": "idea",
         "description": "Idea lifecycle: new (spark), status transitions, "
                        "kill (requires --by reason).",
         "inputSchema": {"type": "object", "properties": {
             "action": {"type": "string",
                        "enum": ["new", "status", "kill"]},
             "target": dict(target_prop),
             "path": {"type": "string"},
             "hypothesis": {"type": "string"},
             "status": {"type": "string"},
             "by": {"type": "string", "description": "kill: reason"},
         }, "required": ["action"]}},
        {"name": "habit",
         "description": "Habit lifecycle: exception (log a missed run), "
                        "revise (--keep or --deprecate). approve is "
                        "user-only.",
         "inputSchema": {"type": "object", "properties": {
             "action": {"type": "string",
                        "enum": ["exception", "revise"]},
             "target": dict(target_prop),
             "why": {"type": "string", "description": "exception reason"},
             "keep": {"type": "boolean"},
             "deprecate": {"type": "boolean"},
         }, "required": ["action", "target"]}},
    ]


MCP_REQUIRED = {
    "recall": ("query",),
    "show": ("target",),
    "list": (),
    "note": ("target", "text"),
    "remember": ("text",),
    "update": ("target", "text"),
    "forget": ("target",),
    "restore": ("target",),
}


def _mcp_dispatch(name, a):
    """Run a tool: build the CLI namespace, capture stdout/stderr, map rc."""
    if not isinstance(a, dict):
        return "invalid arguments (object expected)", True
    if name not in {t["name"] for t in _mcp_tools()}:
        return f"unknown tool: {name}", True
    missing = [k for k in MCP_REQUIRED.get(name, ()) if not a.get(k)]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}", True
    try:
        fn = None
        if name == "recall":
            ns = argparse.Namespace(query=a.get("query"),
                                    limit=a.get("limit"),
                                    type=a.get("type"), json=False)
            fn = cmd_recall
        elif name == "profile":
            ns = argparse.Namespace(scope=a.get("scope"), json=False)
            fn = cmd_profile
        elif name == "digest":
            ns = argparse.Namespace(json=False)
            fn = cmd_digest
        elif name == "show":
            ns = argparse.Namespace(target=a.get("target"))
            fn = cmd_show
        elif name == "list":
            ns = argparse.Namespace(type=a.get("type"),
                                    status=a.get("status"), json=False)
            fn = cmd_list
        elif name == "note":
            ns = argparse.Namespace(target=a.get("target"),
                                    text=a.get("text"), file=None,
                                    side=a.get("side"))
            fn = cmd_note
        elif name == "remember":
            ns = argparse.Namespace(
                text=a.get("text"), file=None, type=a.get("type"),
                path=a.get("path"), scope=a.get("scope"),
                importance=a.get("importance"),
                related_mem=a.get("related_mem"),
                related_mind=a.get("related_mind"),
                author=a.get("author"))
            fn = cmd_remember
        elif name == "update":
            ns = argparse.Namespace(target=a.get("target"),
                                    text=a.get("text"), file=None)
            fn = cmd_update
        elif name in ("forget", "restore"):
            ns = argparse.Namespace(target=a.get("target"))
            fn = cmd_forget if name == "forget" else cmd_restore
        elif name == "veto":
            if not a.get("action") and not a.get("list"):
                return "missing required argument(s): action (or list=true)", \
                    True
            ns = argparse.Namespace(action=a.get("action"),
                                    list=bool(a.get("list")), json=False)
            fn = cmd_veto
        elif name == "idea":
            act = a.get("action")
            if act == "new":
                if not a.get("hypothesis"):
                    return "missing required argument(s): hypothesis", True
                ns = argparse.Namespace(
                    path=a.get("path"), hypothesis=a.get("hypothesis"),
                    file=None, expected=a.get("expected"),
                    criterion=a.get("criterion"), cost=a.get("cost"),
                    related_mem=a.get("related_mem"))
                fn = cmd_idea_new
            elif act == "status":
                if not a.get("target") or not a.get("status"):
                    return "missing required argument(s): target, status", \
                        True
                ns = argparse.Namespace(target=a.get("target"),
                                        status=a.get("status"))
                fn = cmd_idea_status
            elif act == "kill":
                if not a.get("target") or not a.get("by"):
                    return "missing required argument(s): target, by", True
                ns = argparse.Namespace(target=a.get("target"),
                                        by=a.get("by"))
                fn = cmd_idea_kill
            else:
                return "invalid action (expected new|status|kill)", True
        elif name == "habit":
            act = a.get("action")
            if act == "exception":
                if not a.get("target") or not a.get("why"):
                    return "missing required argument(s): target, why", True
                ns = argparse.Namespace(target=a.get("target"),
                                        why=a.get("why"))
                fn = cmd_habit_exception
            elif act == "revise":
                if not a.get("target"):
                    return "missing required argument(s): target", True
                keep, depr = bool(a.get("keep")), bool(a.get("deprecate"))
                if keep == depr:
                    return "exactly one of keep/deprecate is required", True
                ns = argparse.Namespace(target=a.get("target"), keep=keep,
                                        deprecate=depr)
                fn = cmd_habit_revise
            else:
                return "invalid action (expected exception|revise)", True
        else:  # unreachable: tools/list is the same source
            return f"unknown tool: {name}", True
    except (TypeError, ValueError) as e:
        # bad typed arg -> clean tool error, not a JSON-RPC internal fault
        return f"invalid arguments for {name}: {e}", True

    out, err = io.StringIO(), io.StringIO()
    try:
        lock = (MindLock(find_root()) if fn in MUTATING
                else contextlib.nullcontext())
        with lock, contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = fn(ns)
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # never kill the server on a tool error
        return f"{type(e).__name__}: {e}", True
    text = out.getvalue()
    if rc not in (0, None):
        extra = err.getvalue()
        if extra and extra not in text:
            text = text + ("" if not text or text.endswith("\n") else "\n") \
                + extra
        return text or f"error (rc={rc})", True
    return text or err.getvalue() or "ok", False


def _mcp_send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def cmd_serve(args):
    """MCP stdio server: newline-delimited JSON-RPC 2.0 on stdin/stdout."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # Isolation: opencode starts us with the CLIENT project cwd; find_root
    # walks up from there and falls back to HERE (the script IS MIND), so
    # the server always serves this project's store.
    root = find_root()
    print(f"mind serve: root={root}", file=sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _mcp_send({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32700, "message": "parse error"}})
            continue
        # a JSON array/string/number is not a request object (mem audit)
        if not isinstance(msg, dict):
            _mcp_send({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32600,
                                 "message": "invalid request (not an object)"}})
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            if mid is not None:
                _mcp_send({"jsonrpc": "2.0", "id": mid,
                           "error": {"code": -32600,
                                     "message": "invalid request: "
                                                "missing method"}})
            continue
        try:
            if method == "initialize":
                ip = msg.get("params")
                ip = ip if isinstance(ip, dict) else {}
                ver = ip.get("protocolVersion") or MCP_PROTOCOL_DEFAULT
                _mcp_send({"jsonrpc": "2.0", "id": mid, "result": {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": MCP_SERVER_INFO,
                    "instructions": MCP_INSTRUCTIONS}})
            elif method == "ping":
                _mcp_send({"jsonrpc": "2.0", "id": mid, "result": {}})
            elif method == "tools/list":
                _mcp_send({"jsonrpc": "2.0", "id": mid,
                           "result": {"tools": _mcp_tools()}})
            elif method == "tools/call":
                p = msg.get("params")
                if not isinstance(p, dict):
                    _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {
                        "code": -32602,
                        "message": "invalid params (object expected)"}})
                    continue
                args_ = p.get("arguments")
                args_ = args_ if isinstance(args_, dict) else {}
                text, is_err = _mcp_dispatch(p.get("name"), args_)
                _mcp_send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": is_err}})
            elif method.startswith("notifications/"):
                pass  # initialized / cancelled: no response
            elif mid is None:
                pass  # unknown notification: swallow
            else:
                _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {
                    "code": -32601, "message": f"method not found: {method}"}})
        except Exception as e:
            if mid is not None:
                try:
                    _mcp_send({"jsonrpc": "2.0", "id": mid, "error": {
                        "code": -32603, "message": f"{type(e).__name__}: {e}"}})
                except (OSError, ValueError):
                    # double fault: stdout is gone - nothing left to serve
                    return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mind.py",
        description="MIND cognitive layer (traits/habits/ideas/insights)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sync", help="rebuild mind.db from MIND/*.md")
    p.add_argument("--lenient", action="store_true",
                   help="index invalid records with warnings")

    p = sub.add_parser("remember", help="write a new record")
    p.add_argument("text", nargs="?")
    p.add_argument("--file")
    p.add_argument("--type", choices=TYPES)
    p.add_argument("--path")
    p.add_argument("--scope")
    p.add_argument("--importance", type=int)
    p.add_argument("--related-mem", dest="related_mem")
    p.add_argument("--related-mind", dest="related_mind")
    p.add_argument("--author")

    p = sub.add_parser("update", help="new version of a record")
    p.add_argument("target")
    p.add_argument("text", nargs="?")
    p.add_argument("--file")

    p = sub.add_parser("forget", help="tombstone a record")
    p.add_argument("target")

    p = sub.add_parser("restore", help="restore a tombstoned record")
    p.add_argument("target")

    p = sub.add_parser("list", help="list records")
    p.add_argument("--type", choices=TYPES)
    p.add_argument("--status")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show", help="show one record in full")
    p.add_argument("target")

    p = sub.add_parser("link", help="show/edit related_mind links")
    p.add_argument("target")
    p.add_argument("ids", nargs="*")
    p.add_argument("--off", nargs="*", default=[])

    p = sub.add_parser("recall", help="search own records (FTS)")
    p.add_argument("query")
    p.add_argument("--limit", type=int)
    p.add_argument("--type", choices=TYPES)
    p.add_argument("--json", action="store_true")

    pi = sub.add_parser("idea", help="idea lifecycle")
    isub = pi.add_subparsers(dest="action", required=True)
    p = isub.add_parser("new")
    p.add_argument("--path")
    p.add_argument("--hypothesis")
    p.add_argument("--file")
    p.add_argument("--expected")
    p.add_argument("--criterion")
    p.add_argument("--cost")
    p.add_argument("--related-mem", dest="related_mem")
    p = isub.add_parser("status")
    p.add_argument("target")
    p.add_argument("status",
                   choices=("spark", "drafted", "testing", "won", "parked"))
    p = isub.add_parser("kill")
    p.add_argument("target")
    p.add_argument("--by", required=True)

    pt = sub.add_parser("trait", help="trait lifecycle")
    tsub = pt.add_subparsers(dest="action", required=True)
    p = tsub.add_parser("approve")
    p.add_argument("target")

    ph = sub.add_parser("habit", help="habit lifecycle")
    hsub = ph.add_subparsers(dest="action", required=True)
    p = hsub.add_parser("approve")
    p.add_argument("target")
    p = hsub.add_parser("exception")
    p.add_argument("target")
    p.add_argument("--why", required=True)
    p = hsub.add_parser("revise")
    p.add_argument("target")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--keep", action="store_true")
    g.add_argument("--deprecate", action="store_true")

    p = sub.add_parser("note", help="log an observation on a trait")
    p.add_argument("target")
    p.add_argument("text", nargs="?")
    p.add_argument("--file")
    p.add_argument("--side", choices=("up", "down"),
                   help="up: confirms direction, down: argues against")

    sub.add_parser("embed",
                   help="fill mind vectors (remote phone only, slice 1.5)")

    p = sub.add_parser("profile", help="compact self-block")
    p.add_argument("--scope", choices=("agent", "project", "companion"))
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("digest", help="everything pending, one list")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("doctor", help="diagnostics")
    p.add_argument("what", choices=("mem",))
    p.add_argument("--deep", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("veto", help="hard-veto rules for an action")
    p.add_argument("action", nargs="?")   # the command/description to check
    p.add_argument("--list", action="store_true")
    p.add_argument("--json", action="store_true")

    sub.add_parser("serve", help="MCP stdio server (JSON-RPC over stdin)")

    args = ap.parse_args(argv)
    table = {
        "sync": cmd_sync, "remember": cmd_remember, "update": cmd_update,
        "forget": cmd_forget, "restore": cmd_restore, "list": cmd_list,
        "show": cmd_show, "link": cmd_link, "recall": cmd_recall,
        "profile": cmd_profile, "digest": cmd_digest, "doctor": cmd_doctor,
        "veto": cmd_veto, "note": cmd_note, "embed": cmd_embed,
        "serve": cmd_serve,
    }
    if args.cmd == "idea":
        fn = {"new": cmd_idea_new, "status": cmd_idea_status,
              "kill": cmd_idea_kill}[args.action]
    elif args.cmd == "habit":
        fn = {"approve": cmd_habit_approve,
              "exception": cmd_habit_exception,
              "revise": cmd_habit_revise}[args.action]
    elif args.cmd == "trait":
        fn = {"approve": cmd_trait_approve}[args.action]
    else:
        fn = table[args.cmd]
    # single-writer lock around every mutation (read commands stay
    # lockless); MUTATING is defined once with the MCP section above
    try:
        if fn in MUTATING:
            with MindLock(find_root()):
                return fn(args) or 0
        return fn(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
