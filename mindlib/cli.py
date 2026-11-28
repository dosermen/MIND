"""Command-line parser and dispatcher."""
from __future__ import annotations

import argparse
import sys
import traceback

from .core import *
from .veto import cmd_veto
from .bridge_glue import cmd_digest, cmd_doctor
from .mcp import MUTATING, cmd_serve


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
                rc = fn(args) or 0
            # write-path hook (variant (b)): fill vectors now while the
            # phone forward is up - AFTER MindLock, so a mass embed never
            # blocks other writers for minutes; Mind-Embed is the safety net
            if rc == 0 and fn in WRITE_PATH:
                embed_missing_quiet()
            return rc
        return fn(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
