"""Optional Hypothesis properties for the frontmatter parser and sync path."""
import contextlib
import io
import os
import string
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(SRC))
import mind  # noqa: E402

try:
    from hypothesis import given, settings, strategies as st
except ImportError:
    HYPOTHESIS_AVAILABLE = False
else:
    HYPOTHESIS_AVAILABLE = True


class TestFrontmatterProperty(unittest.TestCase):
    if HYPOTHESIS_AVAILABLE:
        _reserved = {
            "id", "type", "path", "status", "version", "author", "updated",
            "scope", "summary", "prediction",
        }
        _alphabet = string.ascii_letters + string.digits + " _./-"
        _keys = st.text(
            alphabet=string.ascii_letters + string.digits + "_",
            min_size=1,
            max_size=12,
        ).filter(lambda key: key not in _reserved)
        _values = st.text(alphabet=_alphabet, max_size=32)

        @settings(max_examples=30, deadline=None)
        @given(st.dictionaries(_keys, _values, max_size=6))
        def test_safe_frontmatter_roundtrips_and_syncs(self, extra):
            fm = {
                "type": "insight",
                "path": "property-test",
                "status": "active",
                "version": 1,
                "author": "test",
                "updated": "2026-11-27",
                "scope": "agent",
                "summary": "generated",
                "prediction": "generated prediction",
                **extra,
            }
            serialized = mind.dump_frontmatter(fm)
            self.assertEqual(mind._lossy_fm(serialized), [])
            parsed, body = mind.parse_frontmatter(serialized)
            self.assertEqual(parsed, fm)
            self.assertEqual(body, "")
            reparsed, _ = mind.parse_frontmatter(
                mind.dump_frontmatter(parsed))
            self.assertEqual(reparsed, fm)

            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                record = root / "MIND" / "insight" / "property-test.md"
                record.parent.mkdir(parents=True)
                record.write_text(serialized, encoding="utf-8")
                previous = os.environ.get("MIND_ROOT")
                os.environ["MIND_ROOT"] = str(root)
                try:
                    with contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(mind.main(["sync"]), 0)
                    db = mind.open_db(root)
                    try:
                        self.assertEqual(
                            db.execute(
                                "SELECT COUNT(*) FROM memories WHERE "
                                "path='property-test'").fetchone()[0],
                            1,
                        )
                    finally:
                        db.close()
                finally:
                    if previous is None:
                        os.environ.pop("MIND_ROOT", None)
                    else:
                        os.environ["MIND_ROOT"] = previous
    else:
        def test_hypothesis_unavailable(self):
            self.skipTest("install hypothesis to run parser property tests")


if __name__ == "__main__":
    unittest.main()
