#!/usr/bin/env python3
"""MIND slice 0 tests: frontmatter, sync, CRUD, lifecycles, recall,
profile/digest, doctor against the stub mem server.

Run from F:\\AI\\MIND:  python -m unittest discover -s tests -v
Every test runs against a temp MIND_ROOT (the real store is never touched).
"""
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
import mind  # noqa: E402

STUB = SRC / "tests" / "stub_mem_server.py"
PINNED_TOOLS = ["remember", "recall", "search", "update_memory", "forget",
                "restore", "list", "show", "link"]


def parse(text):
    return mind.parse_frontmatter(text)


class TestFrontmatter(unittest.TestCase):
    """dump -> parse roundtrip for every shape we use."""

    SAMPLE = {
        "id": 3, "type": "trait", "path": "rigor", "status": "active",
        "version": 2, "author": "mind", "updated": "2026-11-25",
        "confidence": 0.8, "importance": 5, "scope": "agent",
        "value": 0.7,
        "direction": "measure first: 2%/4-4 protocol",
        "falsified_when": "3 corrections in a row say simplify",
        "related_mem": ["23@v2", "76@v3"],
        "related_mind": [12, 15],
        "exceptions": [{"date": "2026-11-20", "why": "ssd path, no defrag"}],
        "resurrected": None,
        "active_flag": True,
    }

    def test_roundtrip(self):
        text = mind.dump_frontmatter(self.SAMPLE) + "body line\n"
        got, body = parse(text)
        self.assertEqual(got, self.SAMPLE)
        self.assertEqual(body, "body line\n")

    def test_no_frontmatter(self):
        fm, body = parse("plain text\n")
        self.assertEqual(fm, {})
        self.assertEqual(body, "plain text\n")

    def test_scalar_related_becomes_list(self):
        fm, _ = parse("---\ntype: idea\nrelated_mem: 76@v3\n---\n")
        self.assertEqual(fm["related_mem"], "76@v3")  # parser keeps scalar
        self.assertEqual(mind._as_list(fm["related_mem"]), ["76@v3"])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        os.environ["MIND_ROOT"] = str(self.root)
        (self.root / "MIND").mkdir()

    def tearDown(self):
        os.environ.pop("MIND_ROOT", None)
        os.environ.pop("STUB_EXTRA_TOOL", None)
        os.environ.pop("STUB_VERSION", None)
        self.tmp.cleanup()

    # -- helpers -------------------------------------------------------
    def run_main(self, *argv):
        buf = io.StringIO()
        # stdout AND stderr: WARN/ERROR were moved to stderr so a --json
        # stdout stays parseable (review MED); tests assert on both
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(buf):
            rc = mind.main(list(argv))
        return rc, buf.getvalue()

    def write_record(self, typ, name, extra, body=""):
        d = self.root / "MIND" / typ
        d.mkdir(parents=True, exist_ok=True)
        fm = {"type": typ, "path": name, "status": "active",
              "version": 1, "author": "test", "updated": "2026-11-25"}
        fm.update(extra)
        p = d / f"{name}.md"
        p.write_text(mind.dump_frontmatter(fm) + body, encoding="utf-8")
        return p

    def sync(self, lenient=False):
        argv = ["sync"] + (["--lenient"] if lenient else [])
        return self.run_main(*argv)

    def id_of(self, typ, name):
        fm, _ = parse((self.root / "MIND" / typ /
                       f"{name}.md").read_text(encoding="utf-8"))
        return fm.get("id")

    def db_row(self, rid):
        con = sqlite3.connect(str(self.root / "mind.db"))
        con.row_factory = sqlite3.Row
        try:
            return con.execute("SELECT * FROM memories WHERE id=?",
                               (rid,)).fetchone()
        finally:
            con.close()

    def write_contract(self, mem_py=None):
        contract = {
            "contract_version": 1,
            "pinned_at": "2026-11-25",
            "mem_py": str(mem_py or STUB),
            "mem_root": str(self.root),
            "protocol": "2024-11-05",
            "serverInfo": {"name": "mem", "version": "2.0.0"},
            "tools": list(PINNED_TOOLS),
            "frontmatter_required": ["type", "path", "status", "version",
                                     "author", "updated"],
            "citation_pattern": r"^\d+@v\d+$",
        }
        (self.root / "CONTRACT.json").write_text(
            json.dumps(contract, indent=2), encoding="utf-8")
        return contract

    def make_insight(self, name, summary="green smoke is not fast"):
        return self.write_record(
            "insight", name,
            {"scope": "agent", "summary": summary,
             "prediction": "always measure tg"})


class TestSync(Base):
    def test_empty_store(self):
        rc, out = self.sync()
        self.assertEqual(rc, 0)
        self.assertIn("synced 0 records", out)
        self.assertTrue((self.root / "MIND.md").is_file())
        self.assertTrue((self.root / "mind.db").is_file())

    def test_assigns_ids_and_stamps(self):
        self.write_record("trait", "rigor",
                          {"scope": "agent", "value": 0.7,
                           "direction": "measure first",
                           "falsified_when": "say simplify 3x"})
        rc, out = self.sync()
        self.assertEqual(rc, 0)
        self.assertIn("1 ids assigned", out)
        fid = self.id_of("trait", "rigor")
        self.assertEqual(fid, 1)
        self.assertTrue((self.root / "MIND.md").read_text(
            encoding="utf-8").count("rigor") >= 1)
        # second sync is quiet: no new ids
        rc, out = self.sync()
        self.assertEqual(rc, 0)
        self.assertIn("0 ids assigned", out)

    def test_strict_rejects_idea_without_criterion(self):
        self.write_record("idea", "bad-test",
                          {"status": "testing", "scope": "project",
                           "hypothesis": "h"})
        rc, out = self.sync()
        self.assertEqual(rc, 1)
        self.assertIn("requires 'criterion'", out)
        row = self.db_row(1)
        self.assertIsNone(row)  # skipped, not indexed
        rc, out = self.sync(lenient=True)
        self.assertEqual(rc, 0)
        self.assertIn("(lenient)", out)
        self.assertIsNotNone(self.db_row(1))

    def test_unknown_type_rejected(self):
        p = self.root / "MIND" / "weird.md"
        p.write_text("---\ntype: dream\npath: x\nstatus: active\n"
                     "version: 1\nauthor: t\nupdated: 2026-11-25\n---\n",
                     encoding="utf-8")
        rc, out = self.sync()
        self.assertEqual(rc, 1)
        self.assertIn("unknown type", out)


class TestCrud(Base):
    def test_idea_new_and_recall(self):
        rc, out = self.run_main("idea", "new", "--path", "kv-loop",
                                "--hypothesis",
                                "loop model fits 12GB at 32k ctx")
        self.assertEqual(rc, 0)
        m = re.search(r"idea (\d+)@v(\d+)", out)
        self.assertIsNotNone(m)
        iid = int(m.group(1))
        self.assertEqual(self.db_row(iid)["type"], "idea")

        rc, out = self.run_main("recall", "loop fits 12GB")
        self.assertEqual(rc, 0)
        self.assertIn("kv-loop", out)
        self.assertIn(f"{iid}@v1", out)

        rc, out = self.run_main("recall", "zzzunmatchzzz")
        self.assertEqual(rc, 1)
        self.assertIn("no hits", out)

    def test_remember_needs_structure(self):
        rc, out = self.run_main("remember", "--type", "insight",
                                "--path", "x", "some text")
        self.assertEqual(rc, 1)
        self.assertIn("requires field", out)

    def test_remember_with_file(self):
        p = self.root / "in.md"
        p.write_text(mind.dump_frontmatter({
            "type": "trait", "path": "novelty", "status": "active",
            "version": 1, "author": "calib", "updated": "2026-11-25",
            "scope": "agent", "value": 0.8,
            "direction": "prefer non-trivial paths",
            "falsified_when": "user says keep it simple twice",
        }) + "legacy import\n", encoding="utf-8")
        rc, out = self.run_main("remember", "--file", str(p))
        self.assertEqual(rc, 0)
        self.assertIn("remembered", out)
        self.assertIn("trait/novelty.md", out)
        rc, out = self.run_main("recall", "non-trivial paths")
        self.assertEqual(rc, 0)
        self.assertIn("novelty", out)

    def test_update_versioning(self):
        rc, out = self.run_main("idea", "new", "--path", "alpha",
                                "--hypothesis", "h1")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("update", str(iid), "new body text")
        self.assertEqual(rc, 0)
        self.assertIn("-> superseded", out)
        new_id = int(re.search(r"updated (\d+)@v(\d+)", out).group(1))
        self.assertNotEqual(new_id, iid)
        old = self.db_row(iid)
        new = self.db_row(new_id)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(new["version"], 2)
        self.assertEqual(new["parent_id"], iid)
        # old file physically superseded, new file exists
        fm_old, _ = parse((self.root / "MIND" / "idea" /
                           "alpha.md").read_text(encoding="utf-8"))
        self.assertEqual(fm_old["status"], "superseded")
        self.assertTrue((self.root / "MIND" / "idea" /
                         "alpha-v2.md").is_file())

    def test_forget_restore_cycle(self):
        rc, out = self.run_main("idea", "new", "--path", "beta",
                                "--hypothesis", "hb")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("forget", str(iid))
        self.assertEqual(rc, 0)
        rc, out = self.run_main("recall", "hb")
        self.assertEqual(rc, 1)  # tombstoned, recall skips it
        rc, out = self.run_main("restore", str(iid))
        self.assertEqual(rc, 0)
        self.assertIn("-> spark", out)
        rc, out = self.run_main("recall", "hb")
        self.assertEqual(rc, 0)

    def test_restore_superseded_needs_no_child_gate(self):
        # restoring a record back to its predelete status superseded is
        # not a resurrection: the live child is not a conflict (review m1)
        rc, out = self.run_main("idea", "new", "--path", "gamma",
                                "--hypothesis", "hg")
        v1 = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("update", str(v1), "v2 body")
        v2 = int(re.search(r"updated (\d+)@", out).group(1))
        rc, out = self.run_main("forget", str(v1))
        self.assertEqual(rc, 0)
        rc, out = self.run_main("restore", str(v1))
        self.assertEqual(rc, 0, out)
        self.assertIn("-> superseded", out)
        self.assertEqual(self.db_row(v1)["status"], "superseded")
        self.assertEqual(self.db_row(v2)["status"], "spark")

    def test_restore_refused_with_active_child(self):
        # two live versions of one lineage (only reachable by hand-editing
        # md): resurrecting the parent would leave an ambiguous pair
        self.write_record("idea", "par", {"status": "drafted",
                                          "scope": "project",
                                          "hypothesis": "h",
                                          "criterion": "c"})
        self.write_record("idea", "kid-v2", {"status": "drafted",
                                             "scope": "project",
                                             "hypothesis": "h",
                                             "criterion": "c",
                                             "supersedes": "idea/par.md"})
        self.sync()
        pid = self.id_of("idea", "par")
        rc, out = self.run_main("forget", str(pid))
        self.assertEqual(rc, 0, out)
        rc, out = self.run_main("restore", str(pid))
        self.assertEqual(rc, 1, out)
        self.assertIn("active child", out)
        self.assertEqual(self.db_row(pid)["status"], "deleted")

    def test_list_and_show(self):
        rc, out = self.run_main("idea", "new", "--path", "delta",
                                "--hypothesis", "hd")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("update", str(iid), "v2")
        v2 = int(re.search(r"updated (\d+)@v(\d+)", out).group(1))
        rc, out = self.run_main("list")
        self.assertEqual(rc, 0)
        self.assertIn("delta-v2", out)
        self.assertNotIn("delta.md", out)  # superseded hidden by default
        rc, out = self.run_main("list", "--status", "all")
        self.assertIn("superseded", out)
        rc, out = self.run_main("show", f"{v2}@v2")
        self.assertEqual(rc, 0)
        self.assertIn("supersedes id", out)  # v2 points at v1

    def test_link_show_off(self):
        a = self.make_insight("one")
        b = self.make_insight("two")
        self.sync()
        ia, ib = self.id_of("insight", "one"), self.id_of("insight", "two")
        rc, out = self.run_main("link", str(ia), str(ib))
        self.assertEqual(rc, 0)
        rc, out = self.run_main("show", str(ia))
        self.assertIn(f"related_mind -> {ib}@v1", out)
        rc, out = self.run_main("link", str(ia), "--off", str(ib))
        self.assertEqual(rc, 0)
        self.assertIn("(none)", out)
        # linking to a missing id fails
        rc, out = self.run_main("link", str(ia), "999")
        self.assertEqual(rc, 1)


class TestIdeaGates(Base):
    def test_transition_and_criterion_gate(self):
        # without criterion: drafted ok, testing blocked
        rc, out = self.run_main("idea", "new", "--path", "no-crit",
                                "--hypothesis", "h")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("idea", "status", str(iid), "testing")
        self.assertEqual(rc, 1)  # spark -> testing not allowed at all
        self.assertIn("not allowed", out)
        rc, out = self.run_main("idea", "status", str(iid), "drafted")
        self.assertEqual(rc, 0)
        rc, out = self.run_main("idea", "status", str(iid), "testing")
        self.assertEqual(rc, 1)
        self.assertIn("criterion required", out)
        # with criterion: full path
        rc, out = self.run_main("idea", "new", "--path", "with-crit",
                                "--hypothesis", "h2",
                                "--criterion", "2 runs, tg >= 20")
        jid = int(re.search(r"idea (\d+)@", out).group(1))
        self.run_main("idea", "status", str(jid), "drafted")
        rc, out = self.run_main("idea", "status", str(jid), "testing")
        self.assertEqual(rc, 0)

    def test_kill_requires_testing_and_reason(self):
        rc, out = self.run_main("idea", "new", "--path", "k1",
                                "--hypothesis", "hk")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("idea", "kill", str(iid), "--by", "x")
        self.assertEqual(rc, 1)  # still spark
        self.assertIn("only a testing idea", out)
        self.run_main("idea", "status", str(iid), "drafted")
        # drafted -> lost is not a transition; must go through testing
        rc, out = self.run_main("idea", "status", str(iid), "parked")
        self.assertEqual(rc, 0)
        # park -> testing needs criterion
        self.run_main("idea", "new", "--path", "k2", "--hypothesis", "hk2",
                      "--criterion", "c")
        kid = int(re.search(r"idea (\d+)@",
                            self.run_main("idea", "new", "--path", "k3",
                                          "--hypothesis", "hk3",
                                          "--criterion", "c")[1]).group(1))
        self.run_main("idea", "status", str(kid), "drafted")
        self.run_main("idea", "status", str(kid), "testing")
        rc, out = self.run_main("idea", "kill", str(kid), "--by",
                                "8.3 t/s at 28k")
        self.assertEqual(rc, 0)
        self.assertIn("testing -> lost", out)
        fm, _ = parse((self.root / "MIND" / "idea" /
                       "k3.md").read_text(encoding="utf-8"))
        self.assertEqual(fm["status"], "lost")
        self.assertEqual(fm["killed_by"], "8.3 t/s at 28k")

    def test_wip_limit(self):
        for i in range(7):
            self.write_record("idea", f"draft-{i}",
                              {"status": "drafted", "scope": "project",
                               "hypothesis": f"h{i}"})
        self.sync()
        rc, out = self.run_main("idea", "new", "--path", "extra",
                                "--hypothesis", "extra")
        eid = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("idea", "status", str(eid), "drafted")
        self.assertEqual(rc, 1)
        self.assertIn("WIP full", out)
        # free a slot, retry
        rc, out = self.run_main("idea", "status",
                                str(self.id_of("idea", "draft-0")),
                                "parked")
        self.assertEqual(rc, 0)
        rc, out = self.run_main("idea", "status", str(eid), "drafted")
        self.assertEqual(rc, 0)


class TestHabit(Base):
    def make_candidate(self):
        self.write_record("habit", "defrag-after-download",
                          {"status": "candidate", "scope": "agent",
                           "trigger": "after model download",
                           "action": "wc-queue + WinContig-Defrag",
                           "confidence": 0.3,
                           "related_mem": ["76@v3"]})
        self.sync()
        return self.id_of("habit", "defrag-after-download")

    def test_lifecycle(self):
        hid = self.make_candidate()
        # candidate must not appear in profile
        rc, out = self.run_main("profile", "--json")
        prof = json.loads(out)
        self.assertEqual(prof["habits"], [])
        rc, out = self.run_main("habit", "approve", str(hid))
        self.assertEqual(rc, 0)
        self.assertIn("candidate -> active", out)
        fm, _ = parse((self.root / "MIND" / "habit" /
                       "defrag-after-download.md").read_text(
                           encoding="utf-8"))
        self.assertEqual(fm["status"], "active")
        self.assertEqual(fm["approved_by"], "user")
        # expires pinned to approve day + 90, not just truthy
        want = (date.today() + timedelta(days=mind.HABIT_EXPIRE_DAYS)
                ).isoformat()
        self.assertEqual(fm["expires"], want)
        # exceptions
        for why in ("ssd path", "manual off", "queue busy"):
            rc, out = self.run_main("habit", "exception", str(hid),
                                    "--why", why)
            self.assertEqual(rc, 0)
        self.assertIn("REVISION FLAG", out)
        rc, out = self.run_main("digest", "--json")
        dig = json.loads(out)
        self.assertEqual(dig["habit_revision"][0]["id"], hid)
        self.assertIn("3 exceptions", dig["habit_revision"][0]["reasons"])
        # profile now shows it
        rc, out = self.run_main("profile", "--json")
        prof = json.loads(out)
        self.assertEqual(len(prof["habits"]), 1)
        # deprecate
        rc, out = self.run_main("habit", "revise", str(hid),
                                "--deprecate")
        self.assertEqual(rc, 0)
        rc, out = self.run_main("profile", "--json")
        self.assertEqual(json.loads(out)["habits"], [])

    def test_exception_only_for_active(self):
        hid = self.make_candidate()
        rc, out = self.run_main("habit", "exception", str(hid),
                                "--why", "too early")
        self.assertEqual(rc, 1)
        self.assertIn("only an active habit", out)


class TestProfileDigest(Base):
    def test_profile_human_and_json(self):
        self.write_record("trait", "rigor",
                          {"status": "active", "scope": "agent",
                           "value": 0.7, "direction": "measure first",
                           "falsified_when": "say simplify 3x",
                           "confidence": 0.8})
        self.make_insight("smoke-tg")
        rc, out = self.run_main("idea", "new", "--path", "parked-one",
                                "--hypothesis", "hp")
        pid = int(re.search(r"idea (\d+)@", out).group(1))
        self.run_main("idea", "status", str(pid), "parked")
        self.run_main("sync")

        rc, out = self.run_main("profile")
        self.assertEqual(rc, 0)
        self.assertIn("rigor=0.7", out)
        self.assertIn("smoke-tg", out)
        self.assertIn("parked 1", out)

        rc, out = self.run_main("profile", "--json")
        prof = json.loads(out)
        self.assertEqual(prof["traits"][0]["path"], "rigor")
        self.assertEqual(prof["idea_counts"], {"parked": 1})

    def test_digest_runs_without_contract(self):
        self.write_record("habit", "h1",
                          {"status": "candidate", "scope": "agent",
                           "trigger": "t", "action": "a"})
        self.sync()
        rc, out = self.run_main("digest")
        self.assertEqual(rc, 0)
        self.assertIn("pending approval (1)", out)
        self.assertIn("mem contract: DOWN", out)

    def test_digest_ready_list(self):
        self.write_record("idea", "ready-one",
                          {"status": "drafted", "scope": "project",
                           "hypothesis": "h", "criterion": "tg >= 20"})
        self.write_record("idea", "not-ready",
                          {"status": "drafted", "scope": "project",
                           "hypothesis": "h2"})
        self.sync()
        rc, out = self.run_main("digest", "--json")
        dig = json.loads(out)
        ready = [r["path"] for r in dig["ideas"]["ready_for_testing"]]
        self.assertEqual(ready, ["ready-one"])


class TestDoctor(Base):
    def test_no_contract(self):
        rc, out = self.run_main("doctor", "mem")
        self.assertEqual(rc, 2)
        self.assertIn("DOWN", out)

    def test_contract_ok_and_deep(self):
        self.write_contract()
        rc, out = self.run_main("doctor", "mem")
        self.assertEqual(rc, 0)
        self.assertIn("contract: OK", out)
        # the server dict is printed as-is; assert the pinned values, no
        # self-fulfilling str.replace (audit: weak assertions)
        self.assertIn("'name': 'mem'", out)
        self.assertIn("'version': '2.0.0'", out)
        rc, out = self.run_main("doctor", "mem", "--deep")
        self.assertEqual(rc, 0)
        self.assertIn("deep recall: 1 hits (76@v3)", out)

    def test_drift_version(self):
        self.write_contract()
        os.environ["STUB_VERSION"] = "9.9.9"
        rc, out = self.run_main("doctor", "mem")
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT", out)
        self.assertIn("serverInfo.version", out)

    def test_drift_added_tool(self):
        self.write_contract()
        os.environ["STUB_EXTRA_TOOL"] = "1"
        rc, out = self.run_main("doctor", "mem")
        self.assertEqual(rc, 1)
        self.assertIn("tools added: shiny_new_tool", out)

    def test_mem_down(self):
        self.write_contract(mem_py=self.root / "no-such-mem.py")
        rc, out = self.run_main("doctor", "mem")
        self.assertEqual(rc, 2)
        self.assertIn("DOWN", out)

    def test_broken_citation_detected(self):
        self.write_contract()
        self.write_record("idea", "cited",
                          {"status": "parked", "scope": "project",
                           "hypothesis": "h",
                           "related_mem": ["76@v3", "99@v1"]})
        self.sync()
        rc, out = self.run_main("digest")
        self.assertEqual(rc, 0)
        self.assertIn("mem contract: OK", out)
        self.assertIn("broken citations: 99@v1", out)

    def test_doctor_json(self):
        self.write_contract()
        rc, out = self.run_main("doctor", "mem", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["state"], "ok")
        self.assertEqual(sorted(data["report"]["tools"]),
                         sorted(PINNED_TOOLS))


class TestBridgeParse(unittest.TestCase):
    def test_parse_recall_text(self):
        mod = mind.load_bridge()
        text = ("[0.8312] 76@v3 semantic inkling2-mini-recon-v3 "
                "(2026-11-25, imp 5) -> memory/semantic/x.md\n"
                "line one\nline two\n"
                "[related -> 12]\n"
                "\n-- 1/75 hits, budget left 5900 B, mode: fts\n")
        hits = mod.MemBridge.parse_recall_text(text)
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual(h["citation"], "76@v3")
        self.assertEqual(h["type"], "semantic")
        self.assertAlmostEqual(h["score"], 0.8312)
        self.assertEqual(h["content"], "line one\nline two")
        self.assertIn("related", h)

    def test_parse_empty(self):
        mod = mind.load_bridge()
        self.assertEqual(mod.MemBridge.parse_recall_text(""), [])
        self.assertEqual(
            mod.MemBridge.parse_recall_text("\n-- 0/75 hits, mode: fts\n"),
            [])


# ---------------------------------------------------------------------------
# P0 hardening tests (audit 26.11: resync, BOM, invariance, validation
# matrix, idea gates matrix, bridge failure modes, real-mem integration)
# ---------------------------------------------------------------------------

class TestP0Resync(Base):
    def test_deletion_triggers_rebuild(self):
        self.make_insight("vanish", summary="remember the titanic fact")
        rc, out = self.sync()
        self.assertEqual(rc, 0)
        rc, out = self.run_main("recall", "titanic")
        self.assertEqual(rc, 0)
        # manual deletion: no other file gets a newer mtime, count drops
        (self.root / "MIND" / "insight" / "vanish.md").unlink()
        rc, out = self.run_main("recall", "titanic")
        self.assertEqual(rc, 1)
        self.assertIn("no hits", out)

    def test_write_inside_old_1ms_window(self):
        # audit: md written <1 ms after the rebuild used to be invisible
        # (mtime compared against db + 0.001). Exact md_newest stamp now.
        p = self.make_insight("late", summary="late writer fact")
        rc, out = self.sync()
        self.assertEqual(rc, 0)
        text = p.read_text(encoding="utf-8")
        p.write_text(text.replace("late writer fact", "rewritten fact"),
                     encoding="utf-8")
        db_mtime = (self.root / "mind.db").stat().st_mtime
        inside = db_mtime + 0.0005      # inside the old +/-1 ms blind spot
        os.utime(p, (inside, inside))
        rc, out = self.run_main("recall", "rewritten")
        self.assertEqual(rc, 0, out)
        self.assertIn("late", out)

    def test_mtime_untouched_but_content_replaced_is_seen(self):
        # same-second rewrite: mtime equality must not mask a change
        p = self.make_insight("same", summary="first variant")
        self.sync()
        db_t = (self.root / "mind.db").stat().st_mtime
        p.write_text(p.read_text(encoding="utf-8").replace(
            "first variant", "second variant"), encoding="utf-8")
        os.utime(p, (db_t, db_t))  # mtime identical to the index
        rc, out = self.run_main("recall", "second")
        self.assertEqual(rc, 0, out)


class TestP0Bom(Base):
    def test_bom_record_is_indexed(self):
        d = self.root / "MIND" / "insight"
        d.mkdir(parents=True, exist_ok=True)
        fm = {"type": "insight", "path": "bom-one", "status": "active",
              "version": 1, "author": "t", "updated": "2026-11-25",
              "scope": "agent", "summary": "bom survives roundtrip",
              "prediction": "sync rc 0"}
        raw = ("\ufeff" + mind.dump_frontmatter(fm) + "body\n")
        (d / "bom-one.md").write_bytes(raw.encode("utf-8"))
        rc, out = self.sync()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.id_of("insight", "bom-one"), 1)
        rc, out = self.run_main("recall", "survives")
        self.assertEqual(rc, 0, out)

    def test_undecodable_file_fails_sync_not_lockout(self):
        d = self.root / "MIND" / "insight"
        d.mkdir(parents=True, exist_ok=True)
        (d / "broken.md").write_bytes(b"\xff\xfe\x00 invalid \x80")
        rc, out = self.sync()
        self.assertEqual(rc, 1)          # reported, not a crash
        self.assertIn("read failed", out)
        rc, out = self.sync(lenient=True)
        self.assertEqual(rc, 0)          # store still usable


class TestP0Invariance(Base):
    def test_update_file_cannot_rewrite_operational_fields(self):
        rc, out = self.run_main("idea", "new", "--path", "inv",
                                "--hypothesis", "h")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        src = self.root / "inv-src.md"
        src.write_text(mind.dump_frontmatter({
            "type": "idea", "path": "hijacked", "status": "spark",
            "id": 999, "version": 99, "supersedes": "attacker.md",
            "updated": "1999-01-01", "hypothesis": "h2",
        }) + "new body\n", encoding="utf-8")
        rc, out = self.run_main("update", str(iid), "--file", str(src))
        self.assertEqual(rc, 0, out)
        m = re.search(r"updated (\d+)@v(\d+)", out)
        new_id = int(m.group(1))
        # design: update mints a NEW record (old -> superseded); the id
        # hijacked from the source file must be ignored
        self.assertNotEqual(new_id, 999)
        self.assertNotEqual(new_id, iid)
        self.assertEqual(int(m.group(2)), 2)      # version = old + 1
        fm, body = parse((self.root / "MIND" / "idea" /
                          "inv-v2.md").read_text(encoding="utf-8"))
        self.assertEqual(fm["path"], "inv")       # path not hijacked
        self.assertEqual(fm["supersedes"], "idea/inv.md")
        self.assertEqual(fm["updated"], date.today().isoformat())
        self.assertNotEqual(fm.get("id"), 999)

    def test_remember_idea_default_status_is_spark(self):
        src = self.root / "idea-src.md"
        src.write_text(mind.dump_frontmatter({
            "type": "idea", "path": "def-spark", "version": 1,
            "author": "t", "updated": "2026-11-25", "scope": "project",
            "hypothesis": "blind default must not be active",
        }) + "b\n", encoding="utf-8")
        rc, out = self.run_main("remember", "--file", str(src))
        self.assertEqual(rc, 0, out)
        fm, _ = parse((self.root / "MIND" / "idea" /
                       "def-spark.md").read_text(encoding="utf-8"))
        self.assertEqual(fm["status"], "spark")


class TestP0ValidationMatrix(Base):
    CASES = [
        ("idea", "bad-status", {"status": "active", "scope": "project",
                                "hypothesis": "h"}, "invalid status"),
        ("trait", "bad-scope", {"scope": "global", "value": 0.5,
                                "direction": "d", "falsified_when": "f"},
         "invalid/missing scope"),
        ("insight", "bad-imp", {"scope": "agent", "summary": "s",
                                "prediction": "p", "importance": 9},
         "importance must be 1..5"),
        ("insight", "bad-conf", {"scope": "agent", "summary": "s",
                                 "prediction": "p", "confidence": 1.5},
         "confidence must be 0..1"),
        ("habit", "no-trigger", {"scope": "agent", "action": "a"},
         "requires field 'trigger'"),
        ("trait", "bad-value", {"scope": "agent", "value": 2.0,
                                "direction": "d", "falsified_when": "f"},
         "trait value must be a number"),
        ("setting", "bad-mode", {"mode": "maybe"},
         "setting mode must be"),
        ("insight", "bad-cite", {"scope": "agent", "summary": "s",
                                 "prediction": "p",
                                 "related_mem": ["not-a-cite"]},
         "suspicious citation"),
    ]

    def test_each_negative_case(self):
        for typ, name, extra, needle in self.CASES:
            with self.subTest(case=name):
                # isolate: a previous case's broken file would fail rc
                import shutil
                shutil.rmtree(self.root / "MIND")
                (self.root / "MIND").mkdir()
                self.write_record(typ, name, extra)
                rc, out = self.sync()
                if "suspicious" in needle:
                    self.assertEqual(rc, 0, out)   # warn only
                    self.assertIn(needle, out)
                else:
                    self.assertEqual(rc, 1, out)
                    self.assertIn(needle, out)


class TestP0IdeaGatesMatrix(Base):
    """Every transition from IDEA_TRANSITIONS, checked in isolation."""

    def make_idea(self, name, status, criterion=None, killed_by=None):
        extra = {"status": status, "scope": "project", "hypothesis": "h"}
        if criterion:
            extra["criterion"] = criterion
        if killed_by:
            extra["killed_by"] = killed_by
        self.write_record("idea", name, extra)
        self.sync()
        return self.id_of("idea", name)

    def test_transition_matrix(self):
        matrix = [
            # (from, to, allowed_by_table)
            ("spark", "drafted", True),
            ("spark", "parked", True),
            ("spark", "testing", False),
            ("drafted", "testing", True),
            ("drafted", "parked", True),
            ("drafted", "won", False),
            ("testing", "won", True),
            ("testing", "lost", False),   # must go through idea kill
            ("parked", "drafted", True),
            ("parked", "testing", True),
            ("won", "drafted", False),
            ("lost", "parked", False),
        ]
        n = 0
        for frm, to, _allowed in matrix:
            n += 1
            iid = self.make_idea(f"m{n}-{frm}", frm,
                                 criterion="crit" if frm != "spark" else None,
                                 killed_by="data" if frm == "lost" else None)
            if to == "lost":
                # `idea status` refuses lost by design (kill only);
                # argparse rejects the choice before we can see rc
                rc, out = self.run_main("update", str(iid), "--file",
                                        str(self.root / "nope.md"))
                continue
            rc, out = self.run_main("idea", "status", str(iid), to)
            expected_ok = to in mind.IDEA_TRANSITIONS[frm]
            self.assertEqual(rc, 0 if expected_ok else 1,
                             f"{frm}->{to}: rc={rc} out={out}")

    def test_criterion_gate_blocks_testing(self):
        iid = self.make_idea("no-crit2", "drafted")
        rc, out = self.run_main("idea", "status", str(iid), "testing")
        self.assertEqual(rc, 1)
        self.assertIn("criterion required", out)

    def test_update_file_status_bypass_blocked(self):
        iid = self.make_idea("bypass", "spark")
        src = self.root / "bypass.md"
        src.write_text(mind.dump_frontmatter({
            "type": "idea", "path": "bypass", "status": "testing",
            "version": 1, "author": "t", "updated": "2026-11-25",
            "scope": "project", "hypothesis": "h", "criterion": "c",
        }) + "x\n", encoding="utf-8")
        rc, out = self.run_main("update", str(iid), "--file", str(src))
        self.assertEqual(rc, 1, out)
        self.assertIn("not allowed", out)
        # record untouched
        fm, _ = parse((self.root / "MIND" / "idea" /
                       "bypass.md").read_text(encoding="utf-8"))
        self.assertEqual(fm["status"], "spark")

    def test_lost_via_update_blocked(self):
        iid = self.make_idea("lost-bypass", "testing", criterion="c")
        src = self.root / "lost-src.md"
        src.write_text(mind.dump_frontmatter({
            "type": "idea", "path": "lost-bypass", "status": "lost",
            "version": 1, "author": "t", "updated": "2026-11-25",
            "scope": "project", "hypothesis": "h", "criterion": "c",
            "killed_by": "x",
        }) + "x\n", encoding="utf-8")
        rc, out = self.run_main("update", str(iid), "--file", str(src))
        self.assertEqual(rc, 1, out)
        self.assertIn("idea kill", out)


class TestP0Recall(Base):
    def test_ranking_recency_and_importance(self):
        old = self.write_record("insight", "old-weak",
                                {"scope": "agent", "summary":
                                 "quantum tunneling gate review",
                                 "prediction": "p", "importance": 1,
                                "updated": "2026-06-01"})
        self.write_record("insight", "new-strong",
                          {"scope": "agent", "summary":
                           "quantum tunneling gate review",
                           "prediction": "p", "importance": 5,
                           "updated": date.today().isoformat()})
        self.sync()
        rc, out = self.run_main("recall", "quantum tunneling", "--json")
        self.assertEqual(rc, 0, out)
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["path"], "new-strong")
        self.assertGreater(rows[0]["score"], rows[1]["score"])

    def test_budget_truncates_json_body(self):
        big = "budget eats this fact " * 400     # > 6000 bytes
        self.write_record("insight", "big-one",
                          {"scope": "agent", "summary": "budget probe",
                           "prediction": "p"}, body=big + "\n")
        self.sync()
        rc, out = self.run_main("recall", "budget", "--json")
        self.assertEqual(rc, 0, out)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertIn("...[truncated]", rows[0]["content"])

    def test_limit_and_dead_query(self):
        for i in range(4):
            self.write_record("insight", f"lim-{i}",
                              {"scope": "agent",
                               "summary": f"limburg cheese fact {i}",
                               "prediction": "p"})
        self.sync()
        rc, out = self.run_main("recall", "limburg", "--limit", "2",
                                "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(json.loads(out)), 2)
        rc, out = self.run_main("recall", " ")
        self.assertEqual(rc, 1)
        self.assertIn("empty query", out)


class TestBridgeFailureModes(unittest.TestCase):
    """spawn/EOF/timeout/allowlist/show semantics of mem-bridge (review)."""

    def make(self, mem_py=None, mem_root=None):
        mod = mind.load_bridge()
        root = Path(tempfile.mkdtemp())
        br = mod.MemBridge(str(mem_py or STUB), str(mem_root or root),
                           timeout=5.0)
        return mod, br, root

    def test_double_start_refused(self):
        mod, br, root = self.make()
        try:
            br.start()
            with self.assertRaises(mod.BridgeError):
                br.start()
        finally:
            br.close()
            root.joinpath("x").write_text("x")  # keep dir for cleanup
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_write_tool_refused(self):
        mod, br, root = self.make()
        try:
            br.start()
            br.handshake()
            with self.assertRaises(mod.BridgeError) as cm:
                br.call("remember", {"text": "x"})
            self.assertIn("read-only", str(cm.exception))
        finally:
            br.close()
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_show_semantics(self):
        mod, br, root = self.make()
        try:
            br.start()
            br.handshake()
            # unknown target -> explicit not found -> None (not an error)
            self.assertIsNone(br.show("999@v1"))
        finally:
            br.close()
        # separate child for the broken knob (env is read at spawn)
        os.environ["STUB_SHOW_BROKEN"] = "1"
        mod, br, root2 = self.make()
        try:
            br.start()
            br.handshake()
            # operational failure -> BridgeError, never a fake None
            with self.assertRaises(mod.BridgeError) as cm:
                br.show("76@v3")
            self.assertIn("not in db", str(cm.exception))
        finally:
            os.environ.pop("STUB_SHOW_BROKEN", None)
            br.close()
            import shutil
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(root2, ignore_errors=True)

    def test_protocol_probe_detects_server_bump(self):
        # the handshake sends a FALSY version; a drifted server reveals
        # its own protocol and check_contract reports the pin mismatch
        os.environ["STUB_PROTOCOL"] = "1999-01-01"
        mod, br, root = self.make()
        try:
            br.start()
            contract = {"protocol": "2024-11-05",
                        "serverInfo": {"name": "mem", "version": "2.0.0"},
                        "tools": list(PINNED_TOOLS)}
            rep = br.check_contract(contract)
            self.assertFalse(rep["ok"])
            self.assertTrue(any("protocol" in d for d in rep["drifts"]),
                            rep["drifts"])
        finally:
            os.environ.pop("STUB_PROTOCOL", None)
            br.close()
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_dead_child_raises_transport_error(self):
        import shutil
        root = Path(tempfile.mkdtemp())
        dead = root / "dead.py"
        dead.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
        mod = mind.load_bridge()
        br = mod.MemBridge(str(dead), str(root), timeout=3.0)
        try:
            br.start()
            with self.assertRaises(mod.TransportError):
                br.handshake()
        finally:
            br.close()
            shutil.rmtree(root, ignore_errors=True)

    def test_mem_root_env_isolation(self):
        # the child must see EXACTLY the contract mem_root: the echo
        # server reports its MEM_ROOT back through serverInfo.version
        import shutil
        root = Path(tempfile.mkdtemp())
        mod = mind.load_bridge()
        pinned = root / "pinned-root"
        pinned.mkdir()
        echo = SRC / "tests" / "echo_mem_server.py"
        br = mod.MemBridge(str(echo), str(pinned), timeout=5.0)
        try:
            br.start()
            info = br.handshake()
            self.assertEqual(info["serverInfo"]["version"], str(pinned))
        finally:
            br.close()
            shutil.rmtree(root, ignore_errors=True)


class TestP0ReviewFixes(Base):
    """Guards for the round-5 review findings (C1/M1-M4, m1-m9)."""

    def test_meta_facts_persist_so_reads_do_not_rewrite(self):
        # C1: meta stamped after commit was rolled back -> every read
        # command rebuilt the store forever
        self.make_insight("keep", summary="persisted facts probe")
        self.sync()
        p = self.root / "MIND" / "insight" / "keep.md"
        before = p.stat().st_mtime_ns
        for _ in range(2):
            rc, out = self.run_main("list")
            self.assertEqual(rc, 0, out)
        self.assertEqual(p.stat().st_mtime_ns, before,
                         "read command rewrote md: drift detection is dead")
        db = sqlite3.connect(str(self.root / "mind.db"))
        try:
            vals = dict(db.execute("SELECT k, v FROM meta").fetchall())
        finally:
            db.close()
        for k in ("md_count", "md_newest", "md_size", "md_max_id"):
            self.assertIn(k, vals)

    def test_deleted_max_id_is_not_recycled(self):
        # M3: deleting the max-id file must not hand its id to a new record
        self.make_insight("first", summary="one")
        self.make_insight("second", summary="two")
        self.sync()
        second_id = self.id_of("insight", "second")
        (self.root / "MIND" / "insight" / "second.md").unlink()
        self.sync()
        rc, out = self.run_main("remember", "--type", "insight", "--path",
                                "third", "--scope", "agent", "--file",
                                str(self._insight_src("third", "three")))
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(self.id_of("insight", "third"), second_id)

    def _insight_src(self, path, summary):
        p = self.root / f"{path}-src.md"
        p.write_text(mind.dump_frontmatter({
            "type": "insight", "path": path, "version": 1, "author": "t",
            "updated": date.today().isoformat(), "scope": "agent",
            "summary": summary, "prediction": "p"}) + "b\n",
            encoding="utf-8")
        return p

    def test_update_resume_after_both_writes(self):
        # M2: a kill after the second write must not turn a retry into
        # "not editable" - the store is already in the final state
        rc, out = self.run_main("idea", "new", "--path", "res",
                                "--hypothesis", "h")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("update", str(iid), "v2")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_main("update", str(iid), "v2")
        self.assertEqual(rc, 0, out)
        self.assertIn("resumed", out)

    def test_remember_refuses_non_entry_status(self):
        # M4: --file must not mint an idea already testing (WIP bypass)
        src = self.root / "byp.md"
        src.write_text(mind.dump_frontmatter({
            "type": "idea", "path": "byp", "status": "testing",
            "version": 1, "author": "t", "updated": "2026-11-25",
            "scope": "project", "hypothesis": "h", "criterion": "c",
        }) + "x\n", encoding="utf-8")
        rc, out = self.run_main("remember", "--file", str(src))
        self.assertEqual(rc, 1, out)
        self.assertIn("must start as", out)

    def test_idea_new_refuses_non_spark(self):
        src = self.root / "spk.md"
        src.write_text(mind.dump_frontmatter({
            "type": "idea", "path": "spk", "status": "drafted",
            "hypothesis": "h", "criterion": "c",
        }) + "x\n", encoding="utf-8")
        rc, out = self.run_main("idea", "new", "--file", str(src))
        self.assertEqual(rc, 1, out)
        self.assertIn("must start as", out)

    def test_kill_requires_nonempty_reason(self):
        # m7: empty --by used to persist a lost idea without killed_by
        rc, out = self.run_main("idea", "new", "--path", "k1",
                                "--hypothesis", "h", "--criterion", "c")
        iid = int(re.search(r"idea (\d+)@", out).group(1))
        self.run_main("idea", "status", str(iid), "drafted")
        self.run_main("idea", "status", str(iid), "testing")
        rc, out = self.run_main("idea", "kill", str(iid), "--by", "  ")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.db_row(iid)["status"], "testing")

    def test_duplicate_live_path_refused(self):
        # m6: two live records sharing a path make lookups ambiguous
        self.make_insight("dup", summary="first")
        self.sync()
        src = self._insight_src("dup", "second")
        rc, out = self.run_main("remember", "--file", str(src))
        self.assertEqual(rc, 1, out)
        self.assertIn("already used", out)

    def test_like_wildcards_are_literal(self):
        # m5: show "a%" must not resolve to an arbitrary record
        self.make_insight("alpha", summary="wildcard probe")
        self.sync()
        rc, out = self.run_main("show", "alp%")
        self.assertEqual(rc, 1, out)
        self.assertIn("not found", out)

    def test_lossy_frontmatter_not_rewritten(self):
        # m4: block scalars are dropped by the parser - never rewrite
        d = self.root / "MIND" / "insight"
        d.mkdir(parents=True, exist_ok=True)
        raw = ("---\ntype: insight\npath: lossy\nstatus: active\n"
               "version: 1\nauthor: t\nupdated: 2026-11-25\n"
               "scope: agent\nsummary: |\n  multi line\n  summary\n"
               "prediction: p\n---\nbody\n")
        f = d / "lossy.md"
        f.write_text(raw, encoding="utf-8")
        rc, out = self.sync()
        self.assertEqual(rc, 1, out)
        self.assertIn("unsupported frontmatter", out)
        self.assertEqual(f.read_text(encoding="utf-8"), raw,
                         "lossy file was rewritten")

    def test_contract_missing_keys_is_down_not_traceback(self):
        # m8: a present-but-incomplete CONTRACT.json must not raise
        (self.root / "CONTRACT.json").write_text('{"protocol": "x"}',
                                                 encoding="utf-8")
        res = mind._mem_contract(self.root, timeout=5.0)
        self.assertEqual(res["state"], "down")
        self.assertIn("missing", res["detail"])

    def test_mindlock_is_reentrant(self):
        with mind.MindLock(self.root):
            with mind.MindLock(self.root):   # nested must not deadlock
                pass
        self.assertFalse((self.root / "mind.lock").exists())


class TestVetoHonesty(Base):
    """User decision 26.11: a mode must name an executor that can enforce it."""

    def setting(self, name, extra):
        self.write_record("setting", name, extra)
        return self.sync()

    def test_block_without_executor_rejected(self):
        rc, out = self.setting("v1", {"mode": "block"})
        self.assertEqual(rc, 1, out)
        self.assertIn("requires veto_executor", out)

    def test_block_with_code_executor_ok(self):
        rc, out = self.setting("v2", {"mode": "block",
                                      "veto_executor": "code"})
        self.assertEqual(rc, 0, out)

    def test_executor_only_meaningful_with_block(self):
        rc, out = self.setting("v3", {"mode": "hint",
                                      "veto_executor": "code"})
        self.assertEqual(rc, 1, out)
        self.assertIn("only meaningful with mode 'block'", out)

    def test_hint_with_none_executor_ok(self):
        rc, out = self.setting("v4", {"mode": "hint",
                                      "veto_executor": "none",
                                      "block_conditions": ["push github"]})
        self.assertEqual(rc, 0, out)

    def test_block_conditions_must_be_list(self):
        rc, out = self.setting("v5", {"mode": "hint",
                                      "block_conditions": "push github"})
        self.assertEqual(rc, 1, out)
        self.assertIn("block_conditions must be a list", out)

    def test_profile_labels_advisory_honestly(self):
        rc, out = self.setting("veto", {"mode": "hint",
                                        "block_conditions": ["push github",
                                                             "shutdown pc"]})
        self.assertEqual(rc, 0, out)
        rc, out = self.run_main("profile")
        self.assertEqual(rc, 0, out)
        self.assertIn("veto=hint", out)
        self.assertIn("NOT enforced", out)
        self.assertIn("executor: none", out)
        self.assertIn("hard-veto condition: push github", out)

    def test_digest_reports_executor_state(self):
        self.setting("veto", {"mode": "hint",
                              "block_conditions": ["push github"]})
        rc, out = self.run_main("digest")
        self.assertEqual(rc, 0, out)
        self.assertIn("6. settings (1)", out)
        self.assertIn("veto=hint", out)
        self.assertIn("NOT enforced", out)


REAL_MEM = Path(r"F:\AI\OLAMA\tools\mem.py")


class TestRealMemIntegration(unittest.TestCase):
    """DESIGN 12: integration smoke against the real mem (skip off-host)."""

    @unittest.skipUnless(
        REAL_MEM.is_file() and (SRC / "CONTRACT.json").is_file(),
        "real mem/CONTRACT.json not available")
    def test_real_doctor_ok_deep(self):
        res = mind._mem_contract(SRC, timeout=25.0, deep=True)
        self.assertEqual(res["state"], "ok",
                         f"detail={res['detail']} report={res['report']}")
        self.assertTrue(res["deep_hits"],
                        "deep recall found nothing in the real store")


if __name__ == "__main__":
    unittest.main()
