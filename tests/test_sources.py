import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from fleet.sources import CliAdapter, DesktopAdapter, ORPHAN_WINDOW, discover_all


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


class ExitedCliSessionTest(unittest.TestCase):
    """A session that exited must still appear for 24h.

    `~/.claude/sessions/<PID>.json` is deleted on exit, so a registry-only
    scan loses the session entirely rather than ageing it into `stale`. The
    spec promises "alive now, plus anything active in the last 24 hours",
    which registry-only discovery can never deliver.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude", "sessions"))
        self.proj = os.path.join(self.home, ".claude", "projects", "-tmp-demo")
        os.makedirs(self.proj)

    def _transcript(self, session_id, cwd="/tmp/demo", age_seconds=0):
        path = os.path.join(self.proj, session_id + ".jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({
                "type": "assistant", "cwd": cwd,
                "timestamp": "2026-08-08T12:00:00.000Z",
                "message": {"model": "claude-opus-5", "content": []},
            }) + "\n")
        if age_seconds:
            old = time.time() - age_seconds
            os.utime(path, (old, old))
        return path

    def _registry(self, pid, session_id):
        path = os.path.join(self.home, ".claude", "sessions", f"{pid}.json")
        with open(path, "w") as fh:
            json.dump({"pid": pid, "sessionId": session_id, "cwd": "/tmp/demo",
                       "name": "demo-a0", "status": "idle",
                       "startedAt": 1786038157970, "updatedAt": 1786038213585}, fh)

    def test_exited_session_is_still_discovered(self):
        self._transcript("gone-1")
        sessions = CliAdapter(self.home).discover()
        self.assertEqual([s.id for s in sessions], ["gone-1"])
        s = sessions[0]
        self.assertIsNone(s.pid, "no pid means the ladder renders it stale")
        self.assertIsNone(s.status_hint)
        self.assertTrue(s.transcript_path.endswith("gone-1.jsonl"))

    def test_cwd_is_read_from_the_transcript_not_the_slug(self):
        """The directory name is a lossy slug; the real cwd is inside the file."""
        self._transcript("gone-2", cwd="/Users/me/Code/my-hyphen-project")
        s = CliAdapter(self.home).discover()[0]
        self.assertEqual(s.cwd, "/Users/me/Code/my-hyphen-project")
        self.assertTrue(s.name.startswith("my-hyphen-project-"), s.name)

    def test_live_session_is_not_duplicated_by_the_orphan_sweep(self):
        self._transcript("live-1")
        self._registry(4242, "live-1")
        sessions = CliAdapter(self.home).discover()
        self.assertEqual(len(sessions), 1, "registry entry must win, not double up")
        self.assertEqual(sessions[0].pid, 4242)
        self.assertEqual(sessions[0].name, "demo-a0")

    def test_transcripts_older_than_the_window_are_skipped(self):
        self._transcript("ancient", age_seconds=ORPHAN_WINDOW + 3600)
        self._transcript("recent", age_seconds=60)
        ids = [s.id for s in CliAdapter(self.home).discover()]
        self.assertEqual(ids, ["recent"])

    def test_unreadable_transcript_still_yields_a_card(self):
        """A transcript with no cwd must degrade, not vanish."""
        path = os.path.join(self.proj, "headless.jsonl")
        with open(path, "w") as fh:
            fh.write("{not json\n")
        s = CliAdapter(self.home).discover()[0]
        self.assertEqual(s.id, "headless")
        self.assertEqual(s.cwd, "")
        self.assertEqual(s.name, "headless"[:8])

    def test_sessions_sharing_a_directory_get_distinct_names(self):
        """Several sessions in $HOME must not render as identical cards."""
        self._transcript("aaaa-1111", cwd="/Users/me")
        self._transcript("bbbb-2222", cwd="/Users/me")
        names = sorted(s.name for s in CliAdapter(self.home).discover())
        self.assertEqual(len(set(names)), 2, names)
        self.assertTrue(all(n.startswith("me-") for n in names), names)


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

    def test_discovers_sessions_in_second_tree(self):
        # Sessions can exist in local-agent-mode-sessions tree too
        alt_sess_dir = os.path.join(
            self.root, "local-agent-mode-sessions", "acct2", "wksp2"
        )
        os.makedirs(alt_sess_dir)
        payload = {
            "sessionId": "local_alt1", "cliSessionId": "xyz",
            "cwd": "/Users/jane", "createdAt": 1785696825173,
            "lastActivityAt": 1785696840358, "model": "claude-opus-5",
            "effort": "medium", "isArchived": False, "title": "Alt tree session",
        }
        with open(os.path.join(alt_sess_dir, "local_alt1.json"), "w") as fh:
            json.dump(payload, fh)
        sessions = DesktopAdapter(self.root).discover()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].name, "Alt tree session")

    def test_corrupt_desktop_session_file_is_skipped(self):
        self.add("u1")
        with open(os.path.join(self.sess_dir, "bad.json"), "w") as fh:
            fh.write("{invalid json")
        # Should skip the corrupt file and still discover the good one
        self.assertEqual(len(DesktopAdapter(self.root).discover()), 1)


class DiscoverAllTest(unittest.TestCase):
    def test_survives_when_one_adapter_raises(self):
        """One adapter's exception does not prevent discovering from others."""
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".claude", "sessions"))
        proj = os.path.join(home, ".claude", "projects", "-test")
        os.makedirs(proj)

        # Add a CLI session so we get at least one result
        payload = {
            "pid": 123, "sessionId": "cli-sess", "cwd": "/tmp",
            "startedAt": 1786038157970, "name": "test-cli",
            "status": "idle", "updatedAt": 1786038213585,
        }
        with open(os.path.join(home, ".claude", "sessions", "123.json"), "w") as fh:
            json.dump(payload, fh)

        # Patch DesktopAdapter to raise when discover() is called.
        # Patch the class as seen by fleet.sources, not by this test module.
        stderr_capture = io.StringIO()
        with contextlib.redirect_stderr(stderr_capture):
            with mock.patch("fleet.sources.DesktopAdapter") as MockDesktop:
                mock_instance = mock.Mock()
                mock_instance.discover.side_effect = RuntimeError("simulated desktop adapter failure")
                MockDesktop.return_value = mock_instance

                # discover_all instantiates DesktopAdapter, gets our mock, calls discover(), catches exception
                sessions = discover_all(home=home, app_support="/tmp")

        # Should still get CLI sessions despite desktop adapter raising
        self.assertGreater(len(sessions), 0)
        cli_sessions = [s for s in sessions if s.source == "cli"]
        self.assertEqual(len(cli_sessions), 1)
        self.assertEqual(cli_sessions[0].name, "test-cli")

        # Verify the exception was logged to stderr by discover_all's traceback.print_exc()
        stderr_output = stderr_capture.getvalue()
        self.assertIn("RuntimeError", stderr_output)
        self.assertIn("simulated desktop adapter failure", stderr_output)


if __name__ == "__main__":
    unittest.main()


class EnvOverrideTest(unittest.TestCase):
    """The two machine-specific roots must be redirectable without editing source.

    Someone with a non-standard layout, a second Claude install, or a copy of
    another machine's data needs a way in that is not "fork it and edit a
    constant". The defaults are read per-instance, not at import, so setting
    the variable after `fleet.sources` is imported still takes effect.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    @contextlib.contextmanager
    def env(self, **pairs):
        with mock.patch.dict(os.environ, pairs, clear=False):
            yield

    def test_cli_home_follows_the_env_var(self):
        with self.env(AGENT_FLEET_HOME=self.tmp):
            self.assertEqual(CliAdapter().home, self.tmp)

    def test_app_support_follows_the_env_var(self):
        with self.env(AGENT_FLEET_APP_SUPPORT=self.tmp):
            self.assertEqual(DesktopAdapter().app_support, self.tmp)

    def test_explicit_argument_beats_the_env_var(self):
        """Callers (and every existing test) pass paths directly; that must win."""
        with self.env(AGENT_FLEET_HOME="/should/not/win",
                      AGENT_FLEET_APP_SUPPORT="/should/not/win"):
            self.assertEqual(CliAdapter(self.tmp).home, self.tmp)
            self.assertEqual(DesktopAdapter(self.tmp).app_support, self.tmp)

    def test_unset_falls_back_to_the_real_home(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("AGENT_FLEET_HOME", "AGENT_FLEET_APP_SUPPORT")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(CliAdapter().home, os.path.expanduser("~"))
            self.assertTrue(
                DesktopAdapter().app_support.endswith(
                    "Library/Application Support/Claude"))

    def test_empty_env_var_is_ignored_not_treated_as_root(self):
        """An exported-but-empty var must not silently point the scan at ''."""
        with self.env(AGENT_FLEET_HOME="", AGENT_FLEET_APP_SUPPORT=""):
            self.assertEqual(CliAdapter().home, os.path.expanduser("~"))
            self.assertTrue(DesktopAdapter().app_support)

    def test_env_var_is_tilde_expanded(self):
        with self.env(AGENT_FLEET_HOME="~/somewhere"):
            self.assertEqual(
                CliAdapter().home, os.path.expanduser("~/somewhere"))
