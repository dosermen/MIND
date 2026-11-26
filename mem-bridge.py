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
import time
from collections import deque
from pathlib import Path

MCP_PROTOCOL = "2024-11-05"


class BridgeError(RuntimeError):
    """mem side failure: spawn, protocol, timeout, tool error."""


class TransportError(BridgeError):
    """Spawn/EOF/timeout/pipe failure: mem itself is unreachable (DOWN)."""


# text header printed by mem cmd_recall:
# [0.8312] 76@v3 semantic inkling2-mini-recon-v3 (2026-11-25, imp 5) -> file
# path is lazy up to " (" (defensive), file may carry " [budget exhausted]"
_RECALL_HDR = re.compile(
    r"^\[([-+0-9.eE]+)\]\s+(\d+)@v(\d+)\s+(\S+)\s+(.+?)\s+"
    r"\(([^,]+),\s*imp\s+(\d+)\)\s+->\s+(.*?)(?:\s+\[budget exhausted\])?\s*$")

# mem cmd_recall footer / related lines (exact shapes from mem.py:1641-1674)
_RECALL_FOOTER = re.compile(r"^-- \d+/\d+ hits\b")
_RELATED_LINE = re.compile(r"^\[related (->|<-)")

# tools the bridge may call: READ-ONLY by contract (DESIGN.md section 10)
_READ_TOOLS = frozenset({"recall", "search", "list", "show"})


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
        self._handshaken = False
        # last stderr lines of the child: attached to errors so a bad
        # MEM_ROOT/typo shows the real reason instead of a bare EOF
        self.stderr_tail = deque(maxlen=20)

    # -- lifecycle ------------------------------------------------------
    def start(self):
        if self.proc is not None:
            raise BridgeError("bridge already started")
        if not self.mem_py.is_file():
            raise TransportError(f"mem.py not found: {self.mem_py}")
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
            raise TransportError(f"cannot spawn mem serve: {e}")
        self._q = queue.Queue()
        proc = self.proc  # capture: close() may null self.proc meanwhile
        threading.Thread(target=self._pump_stdout, args=(proc,),
                         daemon=True).start()
        threading.Thread(target=self._drain_stderr, args=(proc,),
                         daemon=True).start()

    def _pump_stdout(self, proc):
        try:
            for line in proc.stdout:
                self._q.put(line)
        except (OSError, ValueError, AttributeError):
            pass
        self._q.put(None)  # EOF marker

    def _drain_stderr(self, proc):
        # mem prints "mem serve: root=..." to stderr; drain it so the pipe
        # can never fill up and deadlock the child, and keep the tail for
        # error messages (a wrong MEM_ROOT dies with a useful message).
        try:
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip())
        except (OSError, ValueError, AttributeError):
            pass

    def _stderr_hint(self):
        tail = " | ".join(list(self.stderr_tail)[-3:])
        return f" (stderr: {tail})" if tail else ""

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
            raise TransportError(f"mem pipe write failed: {e}"
                                 f"{self._stderr_hint()}")
        # one deadline per request: a chatty stream must not reset the timer
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(
                    f"mem timeout waiting for {method} "
                    f"({self.timeout:.0f}s){self._stderr_hint()}")
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                raise TransportError(
                    f"mem timeout waiting for {method} "
                    f"({self.timeout:.0f}s){self._stderr_hint()}")
            if line is None:
                raise TransportError("mem serve exited (EOF)"
                                     f"{self._stderr_hint()}")
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue  # not JSON-RPC traffic, ignore
            if not isinstance(data, dict) or data.get("id") != mid:
                continue  # notification or foreign response
            err = data.get("error")
            if err:
                err = err if isinstance(err, dict) else {}
                raise BridgeError(f"mem rpc error {err.get('code')}: "
                                  f"{err.get('message')}")
            res = data.get("result", {})
            if not isinstance(res, dict):
                raise BridgeError(f"mem bad result for {method}: "
                                  f"{type(res).__name__}")
            return res

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
        # Send a FALSY protocolVersion on purpose: mem echoes the client's
        # value when present (mem.py:2471 `ip.get(...) or MCP_PROTOCOL`),
        # so sending our own constant would compare it against itself and
        # hide a server-side bump. An unsupported version makes mem reply
        # with ITS real MCP_PROTOCOL, which check_contract then compares
        # against the CONTRACT.json pin (audit 25.11, review MAJOR-1).
        res = self._request("initialize", {
            "protocolVersion": "",
            "capabilities": {},
            "clientInfo": {"name": "mind", "version": "0.1"},
        })
        self.server_info = res.get("serverInfo") or {}
        self.protocol_version = res.get("protocolVersion")
        self._notify("notifications/initialized")
        self._handshaken = True
        return {"serverInfo": self.server_info,
                "protocolVersion": self.protocol_version}

    def list_tools(self):
        res = self._request("tools/list")
        return [t.get("name") for t in res.get("tools", [])
                if isinstance(t, dict)]

    def call(self, name, arguments):
        if name not in _READ_TOOLS:
            # invariant: the bridge never writes to mem (DESIGN.md section 10)
            raise BridgeError(f"refusing non-read-only tool: {name}")
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
        """Return record text, or None when mem says not found.

        Only the exact "not found:" prefix means a missing record; any
        other isError (sqlite locked, "not in db, run sync", ...) is a
        real failure and must not be reported as a broken citation
        (review MAJOR-4).
        """
        out = self.call("show", {"target": str(target)})
        if out["isError"]:
            if out["text"].lstrip().startswith("not found:"):
                return None
            raise BridgeError("mem show failed: "
                              + out["text"].strip()[:300])
        return out["text"]

    @staticmethod
    def parse_recall_text(text):
        """Parse mem's text recall output into hit dicts."""
        hits, cur = [], None
        for line in text.splitlines():
            m = _RECALL_HDR.match(line)
            if m:
                try:
                    score = float(m.group(1))
                except ValueError:
                    score = None
                if score is not None:
                    if cur is not None:
                        hits.append(cur)
                    cur = {"score": score,
                           "id": int(m.group(2)),
                           "version": int(m.group(3)),
                           "citation": f"{m.group(2)}@v{m.group(3)}",
                           "type": m.group(4), "path": m.group(5),
                           "updated": m.group(6).strip(),
                           "importance": int(m.group(7)),
                           "file": m.group(8).strip(), "content": ""}
                    continue
            if cur is None:
                continue
            if _RECALL_FOOTER.match(line):
                hits.append(cur)
                cur = None
                continue
            if _RELATED_LINE.match(line):
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
        if not self._handshaken:
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
        try:
            self.handshake()
        except BaseException:
            self.close()  # never leak the child on a failed handshake
            raise
        return self

    def __exit__(self, *exc):
        self.close()
        return False
