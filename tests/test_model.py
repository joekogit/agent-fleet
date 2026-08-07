import json
import os
import tempfile
import threading
import time
import unittest
from fleet.types import RawSession, TranscriptFacts, ToolCall, SubagentDispatch
from fleet.model import (
    classify, build_card, sort_cards, in_scope, STUCK_AFTER, Collector,
    proc_start_epoch, SYNTHETIC_MODEL,
)

NOW = 1_800_000_000.0


def cli(status="busy", last_activity=NOW, pid=None, started_at=NOW):
    return RawSession(
        id="s1", source="cli", name="demo-a0", cwd="/tmp/demo",
        transcript_path="/tmp/x.jsonl", status_hint=status, pid=pid,
        model=None, effort=None, started_at=started_at,
        last_activity_at=last_activity,
    )


def app(last_activity=NOW):
    return RawSession(
        id="local_1", source="app", name="Morning brief", cwd="/Users/joe",
        transcript_path=None, status_hint=None, pid=None,
        model="claude-opus-5", effort="high", started_at=NOW - 3600,
        last_activity_at=last_activity,
    )


def facts(last_line_at=NOW):
    return TranscriptFacts(last_line_at=last_line_at)


class ClassifyCliTest(unittest.TestCase):
    """PID None means we cannot prove liveness; treat as not-alive."""

    def test_busy_and_writing_is_busy(self):
        status, _ = classify(cli(status="busy", pid=os_pid()), facts(NOW - 5), NOW)
        self.assertEqual(status, "busy")

    def test_busy_but_silent_is_attention(self):
        status, reason = classify(
            cli(status="busy", pid=os_pid()), facts(NOW - STUCK_AFTER - 1), NOW
        )
        self.assertEqual(status, "attention")
        self.assertIn("likely", reason.lower())

    def test_boundary_at_exactly_stuck_after_is_still_busy(self):
        status, _ = classify(
            cli(status="busy", pid=os_pid()), facts(NOW - STUCK_AFTER), NOW
        )
        self.assertEqual(status, "busy")

    def test_idle_process_is_idle(self):
        status, _ = classify(cli(status="idle", pid=os_pid()), facts(NOW - 5), NOW)
        self.assertEqual(status, "idle")

    def test_dead_pid_is_stale(self):
        status, reason = classify(cli(status="busy", pid=999_999), facts(), NOW)
        self.assertEqual(status, "stale")
        self.assertIn("process", reason.lower())

    def test_alive_pid_with_old_activity_timestamp_is_not_stale(self):
        """`updatedAt` is an activity timestamp, not a heartbeat.

        It stops advancing the moment a session goes quiet, so a session idle
        for 13 hours is still perfectly alive. Verified on this machine: three
        live CLI processes, two of them 2.6h and 13.2h since their last
        `updatedAt`. A live PID must never be rendered `stale`.
        """
        status, reason = classify(
            cli(status="idle", pid=os_pid(), last_activity=NOW - 13 * 3600),
            facts(NOW - 13 * 3600), NOW,
        )
        self.assertEqual(status, "idle", reason)

    def test_long_block_on_a_prompt_stays_attention(self):
        """The case the dashboard exists for must not expire.

        A session blocked on a permission prompt for hours is the single most
        important thing on the board. Ageing it out into `stale` dims it and
        sinks it to the bottom exactly when it matters most.
        """
        status, reason = classify(
            cli(status="busy", pid=os_pid(), last_activity=NOW - 6 * 3600),
            facts(NOW - 6 * 3600), NOW,
        )
        self.assertEqual(status, "attention")
        self.assertIn("likely", reason.lower())

    def test_recycled_pid_does_not_resurrect_a_dead_session(self):
        """PID reuse guard, without mocks.

        This test's own process is live and holds `os.getpid()`, but it
        started long after a session claiming to have begun in 2001 — exactly
        the shape of a stale session file whose PID the OS handed to someone
        else. `os.kill(pid, 0)` says "alive"; the process start time says
        "different process".
        """
        raw = cli(status="busy", pid=os_pid(), started_at=NOW - 10 * 365 * 86400)
        status, reason = classify(raw, facts(), NOW)
        self.assertEqual(status, "stale")
        self.assertIn("pid", reason.lower())

    def test_pid_reuse_check_fails_open_without_a_start_time(self):
        """No startedAt means no proof of reuse, and liveness must win.

        Hiding a live session is the failure mode this dashboard cannot
        afford; a session file too old to carry startedAt is still shown.
        """
        raw = cli(status="idle", pid=os_pid(), started_at=None)
        self.assertEqual(classify(raw, facts(), NOW)[0], "idle")


class ProcStartTest(unittest.TestCase):
    def test_reports_a_plausible_start_for_this_process(self):
        now = time.time()
        start = proc_start_epoch(os_pid(), now)
        self.assertIsNotNone(start, "ps must resolve this test process")
        self.assertLessEqual(start, now + 1)
        self.assertGreater(start, now - 86400 * 365)

    def test_unknown_pid_yields_none_not_a_crash(self):
        self.assertIsNone(proc_start_epoch(999_999, time.time()))


class SubagentAttentionTest(unittest.TestCase):
    """A parent blocked on its own sub-agent is working, not waiting.

    Sub-agent turns are never written to the parent's transcript, so the
    parent looks silent for as long as the dispatch runs. Calling that
    "likely waiting on input" is wrong and trains the user to ignore the
    one status that should always mean something.
    """

    def _facts_with_dispatch(self, returned_at):
        f = facts(NOW - STUCK_AFTER - 300)
        f.subagents = [
            SubagentDispatch("a", "Agent", "research", NOW - 400, returned_at=returned_at)
        ]
        return f

    def test_outstanding_dispatch_suppresses_attention(self):
        status, reason = classify(
            cli(status="busy", pid=os_pid()), self._facts_with_dispatch(None), NOW
        )
        self.assertEqual(status, "busy")
        self.assertIn("sub-agent", reason)
        self.assertNotIn("waiting on input", reason)

    def test_returned_dispatch_restores_attention(self):
        status, reason = classify(
            cli(status="busy", pid=os_pid()),
            self._facts_with_dispatch(NOW - 350), NOW,
        )
        self.assertEqual(status, "attention")
        self.assertIn("likely", reason.lower())


class ClassifyAppTest(unittest.TestCase):
    def test_recent_activity_is_busy(self):
        self.assertEqual(classify(app(NOW - 10), facts(), NOW)[0], "busy")

    def test_within_a_day_is_idle(self):
        self.assertEqual(classify(app(NOW - 7200), facts(), NOW)[0], "idle")

    def test_older_than_a_day_is_stale(self):
        self.assertEqual(classify(app(NOW - 90_000), facts(), NOW)[0], "stale")

    def test_app_can_never_be_attention(self):
        """No heartbeat exists for this source, so the heuristic cannot apply."""
        for age in (0, 30, 61, 600, 7200):
            self.assertNotEqual(classify(app(NOW - age), facts(), NOW)[0], "attention")


class BuildCardTest(unittest.TestCase):
    def test_rolls_up_subagents(self):
        f = facts()
        f.subagents = [
            SubagentDispatch("a", "Agent", "one", NOW - 60, returned_at=NOW - 10),
            SubagentDispatch("b", "Agent", "two", NOW - 30, returned_at=None),
        ]
        card = build_card(cli(pid=os_pid()), f, NOW)
        self.assertEqual(card.subagents_running, 1)
        self.assertEqual(card.subagents_done, 1)

    def test_model_comes_from_transcript_when_source_lacks_it(self):
        f = facts()
        f.models_seen = ["claude-opus-5"]
        self.assertEqual(build_card(cli(pid=os_pid()), f, NOW).model, "claude-opus-5")

    def test_synthetic_sentinel_never_wins_the_model_slot(self):
        """'<synthetic>' is what Claude writes for locally-generated turns.

        models_seen is last-wins, so one interrupt or API error at the end of
        a session used to blank the real model AND — since the sentinel has no
        price — the cost estimate with it. Observed live on a session with
        421M cache-read tokens showing 'est. cost —'.
        """
        f = facts()
        f.models_seen = ["claude-opus-5", SYNTHETIC_MODEL]
        f.cache_read_tokens = 421_000_000
        card = build_card(cli(pid=os_pid()), f, NOW)
        self.assertEqual(card.model, "claude-opus-5")
        self.assertIsNotNone(card.cost_estimate)
        self.assertGreater(card.cost_estimate, 0)

    def test_only_synthetic_turns_leave_the_model_unknown(self):
        f = facts()
        f.models_seen = [SYNTHETIC_MODEL]
        self.assertIsNone(build_card(cli(pid=os_pid()), f, NOW).model)

    def test_missing_transcript_yields_card_not_crash(self):
        card = build_card(cli(pid=os_pid()), None, NOW)
        self.assertEqual(card.tokens["input"], 0)
        self.assertIsNone(card.last_tool)

    def test_serializes_last_tool_as_dict(self):
        f = facts()
        f.last_tool = ToolCall(name="Bash", detail="List files", at=NOW - 3)
        card = build_card(cli(pid=os_pid()), f, NOW)
        self.assertEqual(card.last_tool["name"], "Bash")
        self.assertEqual(card.last_tool["detail"], "List files")


class SortAndScopeTest(unittest.TestCase):
    def test_attention_sorts_above_everything(self):
        cards = [
            build_card(cli(status="idle", pid=os_pid()), facts(), NOW),
            build_card(cli(status="busy", pid=os_pid()),
                       facts(NOW - STUCK_AFTER - 5), NOW),
        ]
        self.assertEqual(sort_cards(cards)[0].status, "attention")

    def test_scope_excludes_sessions_older_than_a_day(self):
        self.assertFalse(in_scope(build_card(app(NOW - 90_000), facts(), NOW), NOW))
        self.assertTrue(in_scope(build_card(app(NOW - 3600), facts(), NOW), NOW))


class CollectorConcurrencyTest(unittest.TestCase):
    """Collector.snapshot() must serialize access to the shared TranscriptCache.

    In the real server, a background cache-warm thread and one thread per HTTP
    connection can all call snapshot() at the same moment. TranscriptCache.
    facts_for() does a multi-step read-modify-write on shared, mutable
    per-transcript state (entry.offset, entry.mtime_ns, entry.size,
    entry.facts). Without a lock around snapshot(), two threads can both read
    the same stale entry.offset, both parse the same freshly-appended bytes
    into the same entry.facts, and double-count tokens -- and because
    entry.facts is mutated in place and cached, that corruption persists into
    every future poll, not just the racing ones.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".claude", "sessions"))
        self.proj = os.path.join(self.home, ".claude", "projects", "-tmp-race")
        os.makedirs(self.proj)
        self.session_id = "race-sess"
        self.transcript = os.path.join(self.proj, f"{self.session_id}.jsonl")
        open(self.transcript, "w").close()

        payload = {
            "pid": 999_999_999, "sessionId": self.session_id, "cwd": "/tmp/race",
            "startedAt": time.time() * 1000, "name": "race-a0", "status": "idle",
            "updatedAt": time.time() * 1000,
        }
        with open(os.path.join(self.home, ".claude", "sessions", "999999999.json"), "w") as fh:
            json.dump(payload, fh)

        # Points at a directory that does not exist, so the desktop adapter
        # contributes nothing and every card in the snapshot comes from this
        # one CLI transcript.
        self.app_support = os.path.join(self.home, "no-such-app-support")

    def _append_lines(self, count):
        record = {
            "timestamp": "2026-08-06T00:00:00.000Z",
            "message": {
                "model": "claude-opus-5",
                "usage": {
                    "input_tokens": 1, "output_tokens": 0,
                    "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                },
                "content": [],
            },
        }
        line = json.dumps(record)
        with open(self.transcript, "a") as fh:
            fh.write((line + "\n") * count)

    def _card_tokens(self, snapshot):
        for card in snapshot["cards"]:
            if card["id"] == self.session_id:
                return card["tokens"]["input"]
        raise AssertionError("race session card not found in snapshot")

    def test_concurrent_snapshots_do_not_double_count_tokens(self):
        collector = Collector(home=self.home, app_support=self.app_support)

        # Warm the cache single-threaded, so there is an established entry
        # with a real (non-zero) offset before the race starts.
        self._append_lines(50)
        baseline = self._card_tokens(collector.snapshot())
        self.assertEqual(baseline, 50)

        # New, unread content that every racing thread will compete to
        # consume. Large enough that the read + parse takes measurable wall
        # time, widening the window in which the GIL can switch threads
        # mid-read-modify-write and expose the race.
        self._append_lines(4000)

        n_threads = 16
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()  # force every thread into snapshot() at once
            collector.snapshot()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "worker thread did not finish in time")

        final = self._card_tokens(collector.snapshot())
        self.assertEqual(
            final, 4050,
            "token count diverged from a single consistent parse of the "
            "transcript -- the shared TranscriptCache was corrupted by "
            "concurrent, unsynchronized snapshot() calls",
        )


def os_pid():
    import os
    return os.getpid()


if __name__ == "__main__":
    unittest.main()
