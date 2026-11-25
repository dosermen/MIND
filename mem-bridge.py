#!/usr/bin/env python3
"""mem-bridge.py - the only MIND module that talks to mem (MCP client).

Spawns `python mem.py serve` as a child process and speaks newline-delimited
JSON-RPC 2.0 (MCP 2024-11-05) over stdio. All knowledge about mem's protocol
lives here; the expected shape is pinned in CONTRACT.json and checked by
`mind.py doctor mem` (drift -> warning, never silent compatibility).

mem is touched READ-ONLY: handshake, tools/list, recall, show.
Design contract: DESIGN.md section 10. stdlib only.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

MCP_PROTOCOL = "2024-11-05"


class BridgeError(RuntimeError):
    """mem side failure: spawn, protocol, timeout, tool error."""


# text header printed by mem cmd_recall:
# [0.8312] 76@v3 semantic inkling2-mini-recon-v3 (2026-11-25, imp 5) -> file
_RECALL_HDR = re.compile(
    r"^\[([-+0-9.eE]+)\]\s+(\d+)@v(\d+)\s+(\S+)\s+(\S+)\s+"
    r"\(([^,]+),\s*imp\s+(\d+)\)\s+->\s+(.+?)\s*$")


class MemBridge:
    """One child `mem.py serve`, one stdio JSON-RPC channel."""

    def __init__(self, mem_py, mem_root, timeout=30.0):
        self.mem_py = Path(mem_py)
        self.mem_root = Path(mem_root)
        self.timeout = float(timeout)
        self.proc = None
        self._id = 0
        self._q = queue.Queue()
        self.server_info = None
        self.protocol_version = None

    # -- lifecycle ------------------------------------------------------
    def start(self):
        if not self.mem_py.is_file():
            raise BridgeError(f"mem.py not found: {self.mem_py}")
        env = dict(os.environ)
        # Isolation: mem must serve the pinned root, not markers of our cwd.
        env["MEM_ROOT"] = str(self.mem_root)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(self.mem_py), "serve"],
                cwd=str(self.mem_root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as e:
            raise BridgeError(f"cannot spawn mem serve: {e}")
        self._q = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                self._q.put(line)
        except (OSError, ValueError):
            pass
        self._q.put(None)  # EOF marker

    def _drain_stderr(self):
        # mem prints "mem serve: root=..." to stderr; drain it so the pipe
        # can never fill up and deadlock the child.
        try:
            for _ in self.proc.stderr:
                pass
        except (OSError, ValueError):
            pass

    def _request(self, method, params=None):
        if self.proc is None:
            raise BridgeError("bridge not started")
        self._id += 1
        mid = self._id
        msg = {"jsonrpc": "2.0", "id": mid, "method": method,
               "params": params or {}}
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise BridgeError(f"mem pipe write failed: {e}")
        while True:
            try:
                line = self._q.get(timeout=self.timeout)
            except queue.Empty:
                raise BridgeError(f"mem timeout waiting for {method} "
                                  f"({self.timeout:.0f}s)")
            if line is None:
                raise BridgeError("mem serve exited (EOF)")
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue  # not JSON-RPC traffic, ignore
            if not isinstance(data, dict) or data.get("id") != mid:
                continue  # notification or foreign response
            if "error" in data:
                err = data["error"] or {}
                raise BridgeError(f"mem rpc error {err.get('code')}: "
                                  f"{err.get('message')}")
            return data.get("result", {})

    def _notify(self, method, params=None):
        if self.proc is None:
            return
        try:
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass  # fire-and-forget

    # -- protocol -------------------------------------------------------
    def handshake(self):
        res = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "mind", "version": "0.1"},
        })
        self.server_info = res.get("serverInfo") or {}
        self.protocol_version = res.get("protocolVersion")
        self._notify("notifications/initialized")
        return {"serverInfo": self.server_info,
                "protocolVersion": self.protocol_version}

    def list_tools(self):
        res = self._request("tools/list")
        return [t.get("name") for t in res.get("tools", [])
                if isinstance(t, dict)]

    def call(self, name, arguments):
        res = self._request("tools/call",
                            {"name": name, "arguments": arguments or {}})
        texts = [c.get("text", "") for c in res.get("content", [])
                 if isinstance(c, dict)]
        return {"isError": bool(res.get("isError")),
                "text": "\n".join(texts)}

    def recall(self, query, limit=5, no_vector=True):
        """Run mem recall, return parsed hits (see parse_recall_text).

        no_vector=True by default: the bridge probe must be deterministic
        and network-free - the remote embed path can block ~60 s when the
        phone/adb forward is down.
        """
        out = self.call("recall", {"query": query, "limit": limit,
                                   "no_artifacts": True,
                                   "no_vector": no_vector})
        if out["isError"]:
            raise BridgeError("mem recall failed: "
                              + out["text"].strip()[:300])
        return self.parse_recall_text(out["text"])

    def show(self, target):
        """Return record text, or None when mem says not found."""
        out = self.call("show", {"target": str(target)})
        if out["isError"]:
            return None
        return out["text"]

    @staticmethod
    def parse_recall_text(text):
        """Parse mem's text recall output into hit dicts."""
        hits, cur = [], None
        for line in text.splitlines():
            m = _RECALL_HDR.match(line)
            if m:
                if cur is not None:
                    hits.append(cur)
                cur = {"score": float(m.group(1)),
                       "id": int(m.group(2)), "version": int(m.group(3)),
                       "citation": f"{m.group(2)}@v{m.group(3)}",
                       "type": m.group(4), "path": m.group(5),
                       "updated": m.group(6).strip(),
                       "importance": int(m.group(7)),
                       "file": m.group(8).strip(), "content": ""}
                continue
            if cur is None:
                continue
            if line.startswith("-- "):
                hits.append(cur)
                cur = None
                continue
            if line.startswith("[related"):
                cur["related"] = line
                continue
            cur["content"] += ("" if not cur["content"] else "\n") + line
        if cur is not None:
            hits.append(cur)
        for h in hits:
            h["content"] = h["content"].strip("\n")
        return hits

    # -- contract -------------------------------------------------------
    def check_contract(self, contract):
        """Compare live mem against the pinned CONTRACT.json."""
        drifts = []
        if not self.server_info:
            self.handshake()
        want = contract.get("serverInfo", {}) or {}
        got = self.server_info or {}
        for key in ("name", "version"):
            if key in want and got.get(key) != want.get(key):
                drifts.append(f"serverInfo.{key}: pinned {want.get(key)!r}, "
                              f"got {got.get(key)!r}")
        pinned_proto = contract.get("protocol")
        if pinned_proto and self.protocol_version != pinned_proto:
            drifts.append(f"protocol: pinned {pinned_proto!r}, "
                          f"got {self.protocol_version!r}")
        tools = self.list_tools()
        pinned = list(contract.get("tools", []) or [])
        added = sorted(set(tools) - set(pinned))
        missing = sorted(set(pinned) - set(tools))
        if added:
            drifts.append("tools added: " + ", ".join(added))
        if missing:
            drifts.append("tools missing: " + ", ".join(missing))
        return {"ok": not drifts, "drifts": drifts, "server": got,
                "protocol": self.protocol_version, "tools": tools}

    # -- teardown -------------------------------------------------------
    def close(self):
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        self.proc = None

    def __enter__(self):
        self.start()
        self.handshake()
        return self

    def __exit__(self, *exc):
        self.close()
        return False
