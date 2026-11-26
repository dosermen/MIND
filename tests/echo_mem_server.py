"""Minimal MCP-ish server used by tests: echoes MEM_ROOT back.

Exists as a real file so the test never has to generate (and mis-quote)
Python source at runtime.
"""
import json
import os
import sys


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            res = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mem",
                               "version": os.environ.get("MEM_ROOT",
                                                         "UNSET")},
            }
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}))
            sys.stdout.flush()
        elif method == "tools/list":
            print(json.dumps({"jsonrpc": "2.0", "id": mid,
                              "result": {"tools": []}}))
            sys.stdout.flush()


if __name__ == "__main__":
    main()
