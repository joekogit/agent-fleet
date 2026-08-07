import json
import os
import tempfile
import unittest
from fleet.sources import CliAdapter, DesktopAdapter


class CliAdapterTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude", "sessions"))
        self.proj = os.path.join(self.home, ".claude", "projects", "-tmp-demo")
        os.makedirs(self.proj)

    def add_session(self, pid, session_id, status="idle", name="demo-a0"):
        payload = {
            "pid": pid, "sessionId": session_id, "cwd": "/tmp/demo",
            "startedAt": 1786038157970, "version": "2.1.223", "kind": "interactive",
            "name": name, "status": status, "updatedAt": 1786038213585,
        }
        path = os.path.join(self.home, ".claude", "sessions", f"{pid}.json")
        with open(path, "w") as fh:
            json.dump(payload, fh)

    def test_reads_session_registry(self):
        self.add_session(999, "sess-1", status="busy")
        sessions = CliAdapter(self.home).discover()
        self.assertEqual(len(sessions), 1)
        s = sessions[0]
        self.assertEqual(s.source, "cli")
        self.assertEqual(s.pid, 999)
        self.assertEqual(s.name, "demo-a0")
        self.assertEqual(s.status_hint, "busy")
        self.assertEqual(s.cwd, "/tmp/demo")

    def test_locates_transcript_by_session_id(self):
        self.add_session(999, "sess-1")
        open(os.path.join(self.proj, "sess-1.jsonl"), "w").close()
        s = CliAdapter(self.home).discover()[0]
        self.assertTrue(s.transcript_path.endswith("sess-1.jsonl"))

    def test_missing_transcript_is_none_not_error(self):
        self.add_session(999, "sess-nope")
        self.assertIsNone(CliAdapter(self.home).discover()[0].transcript_path)

    def test_corrupt_session_file_is_skipped(self):
        self.add_session(999, "sess-1")
        with open(os.path.join(self.home, ".claude", "sessions", "bad.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(len(CliAdapter(self.home).discover()), 1)

    def test_missing_root_yields_nothing(self):
        self.assertEqual(CliAdapter("/nonexistent/path").discover(), [])


class DesktopAdapterTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sess_dir = os.path.join(
            self.root, "claude-code-sessions", "acct", "wksp"
        )
        os.makedirs(self.sess_dir)

    def add(self, uuid, title="Morning brief", archived=False):
        payload = {
            "sessionId": f"local_{uuid}", "cliSessionId": "abc",
            "cwd": "/Users/joe", "createdAt": 1785696825173,
            "lastActivityAt": 1785696840358, "model": "claude-opus-5",
            "effort": "high", "isArchived": archived, "title": title,
        }
        with open(os.path.join(self.sess_dir, f"local_{uuid}.json"), "w") as fh:
            json.dump(payload, fh)

    def test_uses_title_as_name(self):
        self.add("u1")
        sessions = DesktopAdapter(self.root).discover()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].name, "Morning brief")
        self.assertEqual(sessions[0].source, "app")
        self.assertIsNone(sessions[0].pid)
        self.assertIsNone(sessions[0].status_hint)

    def test_skips_archived(self):
        self.add("u1", archived=True)
        self.assertEqual(DesktopAdapter(self.root).discover(), [])

    def test_finds_transcript_in_sandboxed_home(self):
        self.add("u1")
        # Real layout: local_<uuid>/.claude/projects/<slug>/<sessionId>.jsonl
        nested = os.path.join(
            self.sess_dir, "local_u1", ".claude", "projects", "-out-abc"
        )
        os.makedirs(nested)
        open(os.path.join(nested, "deadbeef.jsonl"), "w").close()
        s = DesktopAdapter(self.root).discover()[0]
        self.assertTrue(s.transcript_path.endswith("deadbeef.jsonl"))

    def test_no_sandbox_dir_is_none_not_error(self):
        self.add("u1")
        self.assertIsNone(DesktopAdapter(self.root).discover()[0].transcript_path)


if __name__ == "__main__":
    unittest.main()
