#!/usr/bin/env python3
"""Stub mem MCP server for MIND tests (canned protocol responses).

Speaks the same newline-delimited JSON-RPC 2.0 as `mem.py serve`.
Env knobs for drift tests:
  STUB_EXTRA_TOOL=1  -> tools/list gains "shiny_new_tool"
  STUB_VERSION=x.y.z -> serverInfo.version changes
"""
import json
import os
import sys

TOOLS = ["remember", "recall", "search", "update_memory", "forget",
         "restore", "list", "show", "link"]

RECALL_TEXT = (
    "\n[0.8312] 76@v3 semantic inkling2-mini-recon-v3 "
    "(2026-11-25, imp 5) -> memory/semantic/inkling2-mini-recon-v3.md\n"
    "Inkling deleted: ctx collapse at 28k.\n"
    "[related -> 12]\n"
    "\n-- 1/75 hits, budget left 5900 B, "
    "mode: fts (remote server down)\n"
)

SHOW_OK = ("76@v3 [active] semantic inkling2-mini-recon-v3 "
           "(2026-11-25, imp 5, author opencode)\n"
           "file: memory/semantic/inkling2-mini-recon-v3.md\n"
           "\nInkling verdict.\n")


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    tools = list(TOOLS)
    if os.environ.get("STUB_EXTRA_TOOL") == "1":
        tools.append("shiny_new_tool")
    version = os.environ.get("STUB_VERSION") or "2.0.0"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if not isinstance(method, str):
            continue
        if method.startswith("notifications/") or mid is None:
            continue  # no response for notifications
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": (msg.get("params") or {}).get(
                    "protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mem", "version": version}}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"tools": [{"name": t} for t in tools]}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "recall":
                text, is_err = RECALL_TEXT, False
            elif name == "show":
                if str(arguments.get("target")) == "76@v3":
                    text, is_err = SHOW_OK, False
                else:
                    text, is_err = (f"not found: "
                                    f"{arguments.get('target')}"), True
            else:
                text, is_err = "ok", False
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": is_err}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32601, "message": f"method not found: {method}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
