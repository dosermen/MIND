"""MCP stdio server and tool dispatch."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys

from .core import *
from .veto import cmd_veto
from .bridge_glue import cmd_digest, cmd_doctor


MCP_SERVER_INFO = {"name": "mind", "version": "0.2"}


MCP_PROTOCOL_DEFAULT = "2024-11-05"


MCP_INSTRUCTIONS = (
    "MIND cognitive layer: traits, habits, ideas, insights. md files are "
    "the source of truth, mind.db is derived. Honesty gates are enforced "
    "by the code: remember only creates candidates (approve is user-only), "
    "a trait value change consumes its pending notes (recalc rule in "
    "docs/calibration.md). mem itself is read-only through its own MCP.")


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
         "description": "Search MIND records (hybrid by default; "
                        "MIND_RECALL=fts for pure FTS). Footer names "
                        "the mode.",
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
        # write-path hook (variant (b)): quiet, own buffer, after MindLock
        # releases; a dead phone just defers to the Mind-Embed task
        if rc in (0, None) and fn in WRITE_PATH:
            embed_missing_quiet()
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
        if len(line) > MCP_MAX_LINE:
            # bound the parse cost of a runaway line (audit 5.2.3)
            _mcp_send({"jsonrpc": "2.0", "id": None,
                       "error": {"code": -32600,
                                 "message": "invalid request (line too long)"}})
            continue
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
