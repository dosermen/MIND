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
import json
import os
import re
import sqlite3
import sys
import traceback
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
    """Split on commas at depth 0, respecting quotes."""
    parts, depth, cur, q = [], 0, "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in ("\"", "'"):
            q = ch
            cur += ch
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
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

def find_root():
    env = os.environ.get("MIND_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    for p in (cwd, *cwd.parents):
        if ((p / "MIND.md").is_file() or (p / "mind.db").is_file()
                or (p / "MIND").is_dir()):
            return p
    return HERE  # script IS the project (mem deliberately does not do this)


def open_db(root):
    db = sqlite3.connect(str(root / "mind.db"), timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def _newest_md_mtime(root):
    md_dir = root / "MIND"
    if not md_dir.is_dir():
        return None  # no source dir at all
    newest = 0.0
    for f in md_dir.rglob("*.md"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    return newest


def ensure_index(root):
    """Auto-resync when db is missing or any md is newer (drift)."""
    db_path = root / "mind.db"
    if not db_path.is_file():
        _run_sync(root, lenient=True, quiet=True)
        return
    newest = _newest_md_mtime(root)
    if newest is None or newest > db_path.stat().st_mtime + 0.001:
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
        if fm.get("mode") not in ("off", "hint", "block"):
            errors.append(f"{rel}: setting mode must be off|hint|block")
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
    (root / "MIND.md").write_text("\n".join(lines) + "\n",
                                  encoding="utf-8")


def _run_sync(root, lenient=False, quiet=False):
    md_dir = root / "MIND"
    files = sorted(md_dir.rglob("*.md")) if md_dir.is_dir() else []
    errors, warns = [], []
    today = date.today().isoformat()
    entries = []          # (file, fm, body, rel, id)
    seen = {}             # id -> rel
    pending = []          # (file, fm, body, rel) without id yet

    for f in files:
        rel = f.relative_to(md_dir).as_posix()
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            errors.append(f"{rel}: read failed: {e}")
            continue
        fm, body = parse_frontmatter(text)
        typ = fm.get("type")
        if typ not in TYPES:
            errors.append(f"{rel}: missing/unknown type {typ!r}")
            continue
        # stamp defaults (in memory first; written back only for kept files)
        if not fm.get("path"):
            fm["path"] = re.sub(r"-v\d+$", "", f.stem)
        if not fm.get("status"):
            fm["status"] = "active"
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

    # assign ids to new/legacy files (sorted file order, max+1)
    next_id = (max(seen) if seen else 0) + 1
    assigned = 0
    for (f, fm, body, rel) in pending:
        fid = next_id
        next_id += 1
        fm["id"] = fid
        seen[fid] = rel
        entries.append((f, fm, body, rel, fid))
        assigned += 1

    # stamp frontmatter back (id/defaults/normalization)
    for (f, fm, body, rel, _fid) in entries:
        text = dump_frontmatter(fm)
        if body:
            text += body if body.endswith("\n") else body + "\n"
        try:
            if f.read_text(encoding="utf-8") != text:
                write_md(f, fm, body)
        except OSError as e:
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
                "INSERT INTO memories (id,file,path,type,status,content,body,"
                "version,parent_id,importance,confidence,scope,author,"
                "session,created_at,updated_at,related_mind,related_mem,"
                "fm_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                       "VALUES (?,?)", (fid, _build_search_text(fm, body)))
        db.commit()
        write_index(db, root)
    finally:
        db.close()

    for w in warns:
        print(f"WARN: {w}")
    for e in errors:
        print(f"ERROR: {e}")
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
        return db.execute("SELECT * FROM memories WHERE id=?",
                          (int(m.group(1)),)).fetchone()
    t2 = t.replace("\\", "/")
    candidates = (
        ("SELECT * FROM memories WHERE file=?", t2),
        ("SELECT * FROM memories WHERE file=?", t2 + ".md"),
        ("SELECT * FROM memories WHERE path=?", t2),
        ("SELECT * FROM memories WHERE file LIKE ?", "%/" + t2),
        ("SELECT * FROM memories WHERE file LIKE ?", "%/" + t2 + ".md"),
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
    fm, body = parse_frontmatter(fpath.read_text(encoding="utf-8"))
    return {"row": row, "fm": fm, "body": body, "file": fpath,
            "rel": row["file"]}


def save_record(root, rec, fm):
    """Write fm back in place (no version bump) + resync."""
    write_md(rec["file"], fm, rec["body"])
    return _run_sync(root, lenient=False, quiet=True)


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
    for w in warns:
        print(f"WARN: {w}")
    if errs:
        print(f"{where}: validation failed:")
        for e in errs:
            print(f"  - {e}")
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
            raw = Path(args.file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"remember: cannot read {args.file}: {e}")
            return 2
        fm, body = parse_frontmatter(raw)
    else:
        fm, body = {}, args.text or ""
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
    fm.setdefault("status", "active")
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
    target = _collision_free(typ_dir / f"{stem}.md")
    write_md(target, fm, body)
    rc = _run_sync(root, lenient=False, quiet=True)
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
    if rec["row"]["status"] in DEAD:
        print(f"update: record is {rec['row']['status']}, not editable")
        return 1
    old_fm = rec["fm"]
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8")
        except OSError as e:
            print(f"update: cannot read {args.file}: {e}")
            return 2
        src_fm, new_body = parse_frontmatter(raw)
    else:
        src_fm, new_body = {}, args.text or ""
    new_fm = dict(old_fm)
    for k, v in src_fm.items():
        if k in ("id", "version", "supersedes", "updated"):
            continue
        new_fm[k] = v
    new_ver = int(old_fm.get("version", 1)) + 1
    new_fm["version"] = new_ver
    new_fm["supersedes"] = rec["rel"]
    new_fm["updated"] = _today()
    new_fm.pop("id", None)

    errs, warns = validate_record(new_fm, "update")
    if not _print_validation(errs, warns, "update"):
        return 1

    base = re.sub(r"-v\d+$", "", Path(rec["rel"]).stem)
    typ = new_fm["type"]
    new_file = root / "MIND" / typ / f"{base}-v{new_ver}.md"
    if new_file.exists():
        print(f"update: target file already exists: {new_file}")
        return 1
    write_md(new_file, new_fm, new_body)
    old_fm2 = dict(old_fm)
    old_fm2["status"] = "superseded"
    write_md(rec["file"], old_fm2, rec["body"])
    rc = _run_sync(root, lenient=False, quiet=True)
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
    fm = dict(rec["fm"])
    prev = fm.pop("predelete_status", None)
    typ = fm.get("type")
    if prev not in STATUS_MATRIX.get(typ, ()) or prev in DEAD:
        prev = "active" if "active" in STATUS_MATRIX.get(typ, ()) else \
            STATUS_MATRIX.get(typ, ("active",))[0]
    fm["status"] = prev
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
# recall (v0 = FTS, no vectors)
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
        if not rows:
            print("no hits")
            return 1
        bm = [-float(r["b"] or 0.0) for r in rows]
        lo, hi = min(bm), max(bm)
        today = date.today()
        scored = []
        for r, b in zip(rows, bm):
            bn = 1.0 if hi == lo else (b - lo) / (hi - lo)
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
            scored.append((W_BM25 * bn + W_REC * rn + W_IMP * inn, r))
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
              f"mode: fts (vectors: n/a)")
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
            raw = Path(args.file).read_text(encoding="utf-8")
        except OSError as e:
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
    fm.setdefault("status", "spark")
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
    rc = _run_sync(root, lenient=False, quiet=True)
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
    fm["status"] = "lost"
    fm["killed_by"] = args.by
    fm["killed_at"] = _today()
    fm["updated"] = _today()
    rc = save_record(root, rec, fm)
    if rc == 0:
        print(f"idea {rec['row']['id']}: testing -> lost "
              f"(killed_by: {args.by})")
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
                      "mode": json.loads(r["fm_json"]).get("mode")}
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
        print("settings: " + ", ".join(
            f"{s['path']}={s['mode']}" for s in prof["settings"]))
    return 0


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
    cp = root / "CONTRACT.json"
    if not cp.is_file():
        out["detail"] = "no CONTRACT.json"
        return out
    try:
        contract = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out["detail"] = f"bad CONTRACT.json: {e}"
        return out
    try:
        mod = load_bridge()
    except Exception as e:
        out["detail"] = f"bridge load failed: {e}"
        return out
    br = mod.MemBridge(contract.get("mem_py"),
                       contract.get("mem_root"), timeout=timeout)
    try:
        br.start()
        br.handshake()
        report = br.check_contract(contract)
        out["report"] = report
        out["state"] = "ok" if report["ok"] else "drift"
        if deep:
            try:
                hits = br.recall("test", limit=3)
                out["deep_hits"] = [h["citation"] for h in hits]
                for h in hits:
                    if not CITATION_RE.match(h["citation"]):
                        report["drifts"].append(
                            f"bad citation shape: {h['citation']!r}")
                        report["ok"] = False
                        out["state"] = "drift"
            except Exception as e:
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
    finally:
        db.close()

    mem = _mem_contract(root, timeout=20.0, deep=False)

    data = {"date": today.isoformat(), "pending": pending,
            "ideas": {"wip": wip, "ready_for_testing": ready,
                      "parked": parked_n},
            "habit_revision": revision,
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
    print(f"2. ideas: wip {len(wip)}, ready-for-testing {len(ready)}, "
          f"parked {parked_n}")
    for w in wip:
        print(f"   {w['id']} {w['path']} [{w['status']}]")
    print(f"3. habit revision due: {len(revision)}")
    for h in revision:
        print(f"   {h['id']} {h['path']}: {', '.join(h['reasons'])}")
    st = mem["state"]
    if st == "ok":
        print("4. mem contract: OK")
    elif st == "drift":
        print("4. mem contract: DRIFT")
        for d in data["mem"]["drifts"]:
            print(f"   - {d}")
    else:
        print(f"4. mem contract: DOWN ({mem['detail']})")
    broken = mem["broken"]
    if broken is None:
        print("5. broken citations: (mem unavailable, skipped)")
    elif broken:
        print(f"5. broken citations: {', '.join(broken)}")
    else:
        print("5. broken citations: none")
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

    p = sub.add_parser("profile", help="compact self-block")
    p.add_argument("--scope", choices=("agent", "project", "companion"))
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("digest", help="everything pending, one list")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("doctor", help="diagnostics")
    p.add_argument("what", choices=("mem",))
    p.add_argument("--deep", action="store_true")
    p.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    table = {
        "sync": cmd_sync, "remember": cmd_remember, "update": cmd_update,
        "forget": cmd_forget, "restore": cmd_restore, "list": cmd_list,
        "show": cmd_show, "link": cmd_link, "recall": cmd_recall,
        "profile": cmd_profile, "digest": cmd_digest, "doctor": cmd_doctor,
    }
    if args.cmd == "idea":
        fn = {"new": cmd_idea_new, "status": cmd_idea_status,
              "kill": cmd_idea_kill}[args.action]
    elif args.cmd == "habit":
        fn = {"approve": cmd_habit_approve,
              "exception": cmd_habit_exception,
              "revise": cmd_habit_revise}[args.action]
    else:
        fn = table[args.cmd]
    try:
        return fn(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
