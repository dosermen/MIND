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
        with contextlib.redirect_stdout(buf):
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

    def test_restore_refused_with_active_child(self):
        rc, out = self.run_main("idea", "new", "--path", "gamma",
                                "--hypothesis", "hg")
        v1 = int(re.search(r"idea (\d+)@", out).group(1))
        rc, out = self.run_main("update", str(v1), "v2 body")
        v2 = int(re.search(r"updated (\d+)@", out).group(1))
        rc, out = self.run_main("forget", str(v1))
        self.assertEqual(rc, 0)
        rc, out = self.run_main("restore", str(v1))
        self.assertEqual(rc, 1)
        self.assertIn("active child", out)
        self.assertEqual(self.db_row(v1)["status"], "deleted")
        self.assertEqual(self.db_row(v2)["status"], "spark")  # alive idea

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
        self.assertTrue(fm["expires"])
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
        self.assertIn("mem/2.0.0", out.replace("{'name': 'mem', "
                                               "'version': '2.0.0'}",
                                               "mem/2.0.0"))
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


if __name__ == "__main__":
    unittest.main()
