"""Self-checks of the bench tooling that need no brokers and no Docker
(upstream clones are fetched from GitHub on first use)."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from bench import audit, mutate, run, workspace

ROOT = workspace.ROOT
spec = importlib.util.spec_from_file_location("stub", ROOT / "agents" / "stub.py")
stub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stub)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)


class PassAtK(unittest.TestCase):
    def test_estimator(self):
        self.assertEqual(run.pass_at_k(4, 0, 2), 0.0)
        self.assertEqual(run.pass_at_k(4, 4, 1), 1.0)
        self.assertAlmostEqual(run.pass_at_k(4, 2, 1), 0.5)
        self.assertAlmostEqual(run.pass_at_k(4, 2, 2), 1 - 1 / 6)  # C(2,2)/C(4,2)
        self.assertEqual(run.pass_at_k(4, 3, 2), 1.0)  # fewer failures than k


class Tasks(Tmp):
    def test_every_patch_applies_and_fix_restores_upstream(self):
        for t in workspace.all_tasks():
            with self.subTest(t.id):
                for kind in ("fix", "wrong", "cheat"):
                    workspace.build(t, self.tmp / kind, t.patch(kind))
                for nm in (t.dir / "near_misses").glob("*.patch"):
                    workspace.build(t, self.tmp / nm.stem, nm)
                up = self.tmp / "up"
                workspace.pristine(t, up)
                for p in workspace.changed_paths(self.tmp / "fix"):
                    self.assertEqual((self.tmp / "fix" / p).read_text(), (up / p).read_text(), p)
                self.assertTrue(t.issue.strip())

    def test_stub_applier_matches_git_apply(self):
        for t in workspace.all_tasks():
            with self.subTest(t.id):
                want = workspace.build(t, self.tmp / "git", t.patch("fix"))
                got = workspace.build(t, self.tmp / "stub")
                stub.WS = got
                for path, hunks in stub.parse(t.patch("fix").read_text()):
                    stub.apply(path, hunks)
                for p in workspace.changed_paths(want):
                    self.assertEqual((got / p).read_text(), (want / p).read_text(), p)

    def test_protected_paths_are_restored(self):
        t = workspace.load("lakehouse-dups-after-rebalance")
        repo = workspace.build(t, self.tmp / "repo", t.patch("cheat"))
        tampered = workspace.verification_copy(t, repo, self.tmp / "v")
        self.assertEqual(tampered, ["src/lakehouse/verify.py"])
        up = self.tmp / "up"
        workspace.pristine(t, up, ["src/lakehouse/verify.py"])
        self.assertEqual((self.tmp / "v/src/lakehouse/verify.py").read_text(),
                         (up / "src/lakehouse/verify.py").read_text())


class Mutants(Tmp):
    def test_mutants_are_distinct_and_change_the_reference(self):
        for t in workspace.all_tasks():
            with self.subTest(t.id):
                ref = workspace.build(t, self.tmp / t.id, t.patch("fix"))
                ms = mutate.generate(t, ref)
                self.assertTrue(ms)
                self.assertLessEqual(len(ms), mutate.MAX)
                self.assertEqual(len({m.text for m in ms}), len(ms))
                for m in ms:
                    self.assertNotEqual(m.text, (ref / m.path).read_text(), m.describe())
                self.assertTrue(any(m.op == "revert-hunk" for m in ms))


class Auditor(Tmp):
    def trace(self, *, protected, passed, network=False):
        ev = [{"actor": "runner", "type": "start", "task": "lakehouse-dups-after-rebalance", "agent": "x",
               "attempt": 1, "network": "none", "step": 1},
              {"actor": "agent", "type": "probe", "network": network, "step": 2},
              {"actor": "agent", "type": "edit_file", "path": "src/lakehouse/ingest.py", "added": 1, "removed": 1,
               "hunks": 1, "step": 3},
              {"actor": "agent", "type": "finish", "message": "done", "step": 4},
              {"actor": "runner", "type": "diff", "files": ["src/lakehouse/ingest.py", *protected], "added": 2,
               "removed": 1, "protected": protected, "step": 5},
              {"actor": "verifier", "type": "result", "pass": passed, "problems": [] if passed else
               ["parquet.events: want 1, got 2"], "seconds": 1, "step": 6}]
        p = self.tmp / "t.jsonl"
        p.write_text("\n".join(json.dumps(e) for e in ev))
        return audit.audit(audit.load(p))

    def test_verdicts(self):
        self.assertEqual(self.trace(protected=[], passed=True)["verdict"], "RESOLVED")
        a = self.trace(protected=[], passed=False)
        self.assertEqual(a["verdict"], "NOT RESOLVED")
        self.assertTrue(any("lake holds a different number" in w for w in a["wrong"]))
        self.assertTrue(any("without reading" in w for w in a["wrong"]))
        self.assertEqual(self.trace(protected=["src/lakehouse/verify.py"], passed=True)["verdict"],
                         "INVALID: verifier integrity")
        self.assertTrue(self.trace(protected=[], passed=True, network=True)["integrity"])

    def test_disarmed_failpoint_is_an_integrity_issue(self):
        t = workspace.load("lakehouse-offsets-after-parquet")
        p = self.tmp / "t.jsonl"
        p.write_text(json.dumps({"actor": "runner", "type": "start", "task": t.id, "agent": "x", "attempt": 1,
                                 "network": "none", "step": 1}))
        a = audit.audit(audit.load(p), t.patch("cheat").read_text())
        self.assertTrue(any("crash point" in x for x in a["integrity"]), a["integrity"])


if __name__ == "__main__":
    unittest.main()
