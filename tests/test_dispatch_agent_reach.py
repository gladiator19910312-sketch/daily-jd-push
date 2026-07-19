import importlib.util
import json
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "dispatch_agent_reach.py"
SPEC = importlib.util.spec_from_file_location("dispatch_agent_reach", SCRIPT)
dispatcher = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(dispatcher)


class AgentReachDispatcherTests(unittest.TestCase):
    def test_workflow_command_uses_argv_and_explicit_safe_inputs(self):
        command = dispatcher.workflow_command(
            "c2FuaXRpemVk",
            workflow="daily-job-radar.yml",
            ref="main",
            dry_run=False,
            force_all=True,
            dispatch_id="ar-test-123",
        )

        self.assertEqual(command[:4], ["gh", "workflow", "run", "daily-job-radar.yml"])
        self.assertIn("dry_run=false", command)
        self.assertIn("force_all=true", command)
        self.assertIn("agent_reach_supplement_b64=c2FuaXRpemVk", command)
        self.assertIn("dispatch_id=ar-test-123", command)
        self.assertNotIn("--shell", command)

    @patch.object(dispatcher.time, "sleep")
    @patch.object(dispatcher, "_run")
    def test_run_lookup_matches_unique_dispatch_title(self, mock_run, _mock_sleep):
        rows = [
            {
                "databaseId": 1,
                "displayTitle": "Agent Reach ar-other",
                "createdAt": "2026-07-19T14:00:01Z",
            },
            {
                "databaseId": 2,
                "displayTitle": "Agent Reach ar-target",
                "createdAt": "2026-07-19T14:00:02Z",
            },
        ]
        mock_run.return_value = subprocess.CompletedProcess(
            ["gh"], 0, json.dumps(rows), ""
        )

        found = dispatcher._find_dispatched_run(
            "daily-job-radar.yml",
            "ar-target",
            datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(found["databaseId"], 2)


if __name__ == "__main__":
    unittest.main()
