import unittest
from fleet.types import RawSession, TranscriptFacts, ToolCall, SubagentDispatch
from fleet.model import classify, build_card, sort_cards, in_scope, STUCK_AFTER

NOW = 1_800_000_000.0


def cli(status="busy", last_activity=NOW, pid=None):
    return RawSession(
        id="s1", source="cli", name="demo-a0", cwd="/tmp/demo",
        transcript_path="/tmp/x.jsonl", status_hint=status, pid=pid,
        model=None, effort=None, started_at=NOW - 3600,
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

    def test_alive_pid_with_old_heartbeat_is_stale(self):
        # Guards against PID reuse making a dead session look alive.
        status, _ = classify(
            cli(status="busy", pid=os_pid(), last_activity=NOW - 3600), facts(), NOW
        )
        self.assertEqual(status, "stale")


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


def os_pid():
    import os
    return os.getpid()


if __name__ == "__main__":
    unittest.main()
