#!/usr/bin/env python3
"""MIND - cognitive layer: traits, habits, ideas, insights (slice 0).

Contract: DESIGN.md (this directory). Principles copied from mem:
  * md files under MIND\\ are the source of truth; mind.db is derived
    (full rebuild on sync, auto-resync on drift);
  * versions = new file + supersedes (old -> superseded), tombstone
    delete, citations id@vN, related links without a version bump;
  * one-way read-only bridge to mem via mem-bridge.py (MCP client).
stdlib only, modular package (mem principle). ASCII in code.
"""
from __future__ import annotations

import argparse
import contextlib
from contextlib import contextmanager
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
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

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
                lines.append(f"{k}:")
                lines.extend(f"  - {_fmt(x)}" for x in v)
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
    body = body or ""
    if body and not body.endswith("\n"):
        body += "\n"
    text = dump_frontmatter(fm) + body
    atomic_write(path, text)


def atomic_write(path, text):
    """Write UTF-8 text through a synced temp file and atomic replacement."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f".tmp{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    tmp = path.with_name(path.name + suffix)
    try:
        with tmp.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(tmp), str(path))
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some platforms (notably Windows) cannot open directories for
            # fsync; the file itself is synced and replace remains atomic.
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


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

# ReDoS budget (audit 5.2.1): veto rules are local, but CPython re has
# no timeout - a catastrophic pattern against a huge string hangs the
# command (and the plugin's 60s cache refresh with it). Bound the input;
# commands longer than this are not real shell one-liners anyway.
VETO_ACTION_MAX = 4096

# serve input bound (audit 5.2.3): stdio-local, but a runaway client
# must not make the server buffer an unbounded line
MCP_MAX_LINE = 1 * 1024 * 1024

# in-process reentrancy: mutations hold the lock via main(), while
# ensure_index/_run_sync called from READ commands must take it too
# (review M1: reads rebuilt the store with no lock at all)
_LOCK_HELD = False
# True only while this process owns the outermost MindLock acquisition.
# Process-wide (not thread-local): serve dispatches sequentially and one
# root is active at a time, so nested acquisitions always share a root.


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
            except (OSError, UnicodeDecodeError):
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
    atomic_write(root / "MIND.md", out)


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

    def flow_kind(value):
        if not value.startswith(("{", "[")):
            return None
        opening = value[0]
        closing = "}" if opening == "{" else "]"
        depth, quote, escaped, end = 0, None, False, None
        for index, char in enumerate(value):
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\" and quote == '"':
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in ("\"", "'"):
                quote = char
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    end = index
                    break
        if end is None or value[end + 1:].strip():
            return None
        if opening == "{":
            return "flow mapping"

        quote, escaped, nested, structured = None, False, False, False
        for char in value[1:end]:
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\" and quote == '"':
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in ("\"", "'"):
                quote = char
            elif char in "[{":
                nested = True
            elif char in ",:":
                structured = True
        return "flow sequence" if nested or structured else None

    def inspect_value(value, line, key=None, allow_mapping=False,
                      allow_single_bracket=True):
        if re.fullmatch(r"[|>][-+]?\d*", value):
            bad.append(f"line {line} block scalar {value!r}")

        unquoted = not value.startswith(("\"", "'"))
        if unquoted:
            if key == "<<" and value.startswith("*"):
                bad.append(f"line {line} merge key/alias")
            syntax = re.search(
                r"(?:^|[:,]\s*|\[\s*)(!![^\s,}\]]+|"
                r"[&*][A-Za-z0-9_-]+)(?=\s|[,}\]]|$)", value)
            if syntax:
                token = syntax.group(1)
                kind = ("tag" if token.startswith("!!") else
                        "anchor" if token.startswith("&") else "alias")
                bad.append(f"line {line} {kind}")
            flow = flow_kind(value)
            if not flow and not allow_single_bracket and \
                    value.startswith("["):
                flow = "flow sequence"
            if flow and not (allow_mapping and flow == "flow mapping"):
                bad.append(f"line {line} {flow}")

        quote, escaped = None, False
        for index, char in enumerate(value):
            if escaped:
                escaped = False
                continue
            if char == "\\" and quote == '"':
                escaped = True
                continue
            if char in ("\"", "'"):
                quote = None if quote == char else char
                continue
            if char == "#" and quote is None and (
                    index == 0 or value[index - 1].isspace()) and not (
                    index + 1 < len(value)
                    and value[index + 1].isdigit()):
                bad.append(f"line {line} inline comment")
                break

    for i, raw in enumerate(m.group(1).splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        s = raw.strip()
        if raw[0] in (" ", "\t"):
            if s == "-" or s.startswith("- "):
                value = s[1:].strip()
                if value:
                    inspect_value(value, i, allow_mapping=True,
                                  allow_single_bracket=False)
                continue
            bad.append(f"indented line {i} (continuation/block scalar)")
            continue
        if ":" not in raw:
            bad.append(f"line {i} without ':' (dropped by parser)")
            continue
        key, _, v = raw.partition(":")
        key, v = key.strip(), v.strip()
        if v:
            inspect_value(v, i, key=key)
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


def env_flag(name, default="1"):
    """Truthy unless the env says 0/off/false/no. Explicit strings only:
    `if os.environ.get(x)` would read the literal 'False' as ENABLED (mem)."""
    return str(os.environ.get(name, default)).strip().lower() not in (
        "0", "off", "false", "no", "")


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# write-path hook (28.11, variant (b), DESIGN 13): these four commands fill
# vectors right after a mutation while the phone forward is already up; the
# Mind-Embed task is the safety net for everything else and for dead remotes.
WRITE_PATH = {cmd_remember, cmd_update, cmd_forget, cmd_restore}


def health_url():
    """GET /health on the same origin as the embeddings URL: the adb
    forward keeps listening even when the phone-side llama-server is dead -
    only /health tells the truth (mem 17.11)."""
    try:
        parts = urllib.parse.urlsplit(_embed_url())
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/health"
    except ValueError:
        pass
    return "http://127.0.0.1:5031/health"


def server_ready(timeout=3):
    """200 when the model is loaded (or sleeping: /health answers 200 and
    the embeddings call itself wakes it up)."""
    try:
        with _opener.open(health_url(), timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pid_alive(pid):
    """Windows-safe liveness probe. NEVER os.kill(pid, 0) on Windows: it
    maps to TerminateProcess and would kill the lock owner (mem 16.11)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            # 0x00100000 = SYNCHRONIZE (GENERIC_ALL made an access-denied
            # live owner look dead and the lock got stolen - mem round-2)
            handle = k32.OpenProcess(0x00100000, False, pid)
            if not handle:
                return False
            try:
                # WAIT_TIMEOUT (0x102) = still running, WAIT_OBJECT_0 = dead
                return k32.WaitForSingleObject(handle, 0) == 0x102
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return True               # can't tell -> respect the lock
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


@contextmanager
def embed_lock():
    """Interlock with Phone-Embed-Watchdog: a mass embed legitimately holds
    the phone busy for minutes, and a mid-request respawn looked like a
    degraded cgroup (mem 15.11). The lock file next to mind.db tells the
    watchdog to skip its latency probe/respawn while embed runs. Yields
    True when the caller may embed, False when a live owner holds it."""
    lock = Path(str(find_root() / "mind.db") + ".embed.lock")
    # clamp: ttl <= 0 would expire every lock instantly and let a second
    # run take over a live one (mem round-2 audit)
    ttl = max(1.0, env_float("MIND_EMBED_LOCK_S", 3600))

    def _hold():
        """True while another run legitimately owns the lock."""
        try:
            if (time.time() - lock.stat().st_mtime) >= ttl:
                return False              # TTL expired: take over
            owner = int(lock.read_text(encoding="ascii").strip())
        except OSError:
            return False                  # vanished: nobody holds it
        except ValueError:
            return True                   # fresh but unreadable: respect it
        return owner < 0 or _pid_alive(owner)

    acquired = False
    broken = False
    try:
        # atomic O_EXCL create: exists()+write_text lets two racers both
        # pass and both embed (mem round-2 audit)
        for _ in range(3):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                if _hold():
                    break                 # a live owner keeps it
                try:
                    lock.unlink()         # stale (dead owner / expired)
                except OSError:
                    pass
    except OSError:
        broken = True                     # lock not writable: run unlocked
    try:
        yield acquired or broken
    finally:
        if acquired:
            try:
                lock.unlink()
            except OSError:
                pass


def embed_missing_quiet():
    """Write-path hook (mem 1.3 port): fill vectors right after a
    remember/update/forget/restore while the phone forward is already up.
    Quiet and best effort - nothing pending means no network, a dead remote
    or a busy lock just defers (the next Mind-Embed tick finishes it).
    Never raises: a mutation must not fail because embedding did not."""
    if not env_flag("MIND_EMBED_HOOK", "1"):
        return
    # health (3 s) instead of a bare TCP probe: the adb forward listens
    # even when the phone-side llama is dead - only /health tells the truth
    if not server_ready(timeout=3):
        return
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            cmd_embed(argparse.Namespace(force=False))
    except Exception:
        return          # server went down mid-run: recall degrades to FTS


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
    Single-flight via embed_lock(): the watchdog must not respawn the phone
    llama mid-request (mem 15.11).
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
        with embed_lock() as locked:
            if not locked:
                print("embed: another run holds the lock, skipping")
                return 0
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for i in range(0, len(todo), EMBED_BATCH):
                batch = todo[i:i + EMBED_BATCH]
                # health before every batch: a peer that accepted the
                # first request may hang mid-run - fail fast, fill later
                if not server_ready(timeout=3):
                    raise RuntimeError("health probe failed")
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
# recall (v0 = FTS; hybrid default since A/B gate passed 28.11.2026,
# slice 1.5; MIND_RECALL=fts opts back out to pure FTS)
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

        # --- hybrid half (slice 1.5): cosine over mind_vec. Default is
        # hybrid since the A/B usefulness gate passed (DESIGN 13,
        # 28.11.2026: hit@5 8/10 vs FTS 5/10, MRR 5.17 vs 3.83 on 12
        # records). MIND_RECALL=fts opts back out to pure FTS.
        mode = "fts (vectors: n/a)"
        cos_by_id = {}
        if os.environ.get("MIND_RECALL", "hybrid").lower() == "fts":
            mode = "fts (opt-out)"   # vectors may exist; user chose FTS
        else:
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


















































































































































































































































































































































































































































































# ---------------------------------------------------------------------------
# doctor mem
# ---------------------------------------------------------------------------


































# ---------------------------------------------------------------------------
# MCP serve (slice 1): newline-delimited JSON-RPC over stdio, the same
# shape as mem.py serve - opencode plugs it into opencode.jsonc
# ---------------------------------------------------------------------------










# everything the CLI defines; filled below the cmd_* definitions

































































































































































































































































































































































# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------





















































































































































