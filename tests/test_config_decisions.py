"""Config rules decided after the 2026-09-27 review (CS-313, CS-319).

CS-313: the scheduled job runs at most every five minutes and every half hour
by default; an older, shorter period is raised by `config upgrade` rather than
left to make every command exit 4.

CS-319: a true/false setting must be a real TOML boolean. `bool("false")` is
True, so a quoted value used to mean the opposite of what it said.
"""
from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.config import parse_config_text
from codexsync.config_migrate import (
    FINDING_CODES,
    SCHEDULER_INTERVAL_TOO_SHORT,
    inspect_config,
    render_migrated_text,
)
from codexsync.exceptions import ConfigError
from codexsync.models import (
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    MIN_SCHEDULER_INTERVAL_SECONDS,
    SchedulerConfig,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SANDBOX = REPO_ROOT / "test-sandbox"

BASE = (
    "[paths]\n"
    'cloud_root_dir = "sync"\n'
    'backup_dir = "backups"\n'
    'temp_dir = ".tmp"\n'
)


def _parse(extra: str = ""):
    return parse_config_text(BASE + extra, base_dir=SANDBOX / "unused")


class SchedulerPeriodTests(unittest.TestCase):
    def test_default_is_half_an_hour_and_minimum_five_minutes(self) -> None:
        self.assertEqual(DEFAULT_SCHEDULER_INTERVAL_SECONDS, 1800)
        self.assertEqual(MIN_SCHEDULER_INTERVAL_SECONDS, 300)
        self.assertEqual(SchedulerConfig().interval_seconds, 1800)
        self.assertEqual(_parse().scheduler.interval_seconds, 1800)

    def test_a_shorter_period_is_a_blocker_that_upgrade_raises_to_the_minimum(self) -> None:
        text = BASE + "[scheduler]\ninterval_seconds = 60\n"
        with self.assertRaisesRegex(ConfigError, "config upgrade"):
            _parse("[scheduler]\ninterval_seconds = 60\n")
        plan = inspect_config(text)
        self.assertIn(SCHEDULER_INTERVAL_TOO_SHORT, plan.codes())
        self.assertIn(SCHEDULER_INTERVAL_TOO_SHORT, [item.code for item in plan.blockers])
        upgraded = render_migrated_text(text, plan)
        cfg = parse_config_text(upgraded, base_dir=SANDBOX / "unused")
        self.assertEqual(cfg.scheduler.interval_seconds, MIN_SCHEDULER_INTERVAL_SECONDS)

    def test_an_allowed_period_is_left_alone(self) -> None:
        plan = inspect_config(BASE + "[scheduler]\ninterval_seconds = 300\n")
        self.assertNotIn(SCHEDULER_INTERVAL_TOO_SHORT, plan.codes())

    def test_the_code_is_known_to_the_window(self) -> None:
        self.assertIn(SCHEDULER_INTERVAL_TOO_SHORT, FINDING_CODES)


class StrictBooleanTests(unittest.TestCase):
    def test_a_quoted_boolean_is_refused_and_named(self) -> None:
        cases = {
            '[backup]\nbackup_before_overwrite = "false"\n': "backup.backup_before_overwrite",
            '[safety]\nrequire_codex_stopped = "false"\n': "safety.require_codex_stopped",
            '[sync]\ndry_run_default = "no"\n': "sync.dry_run_default",
            "[conflict]\nreport_conflicts = 1\n": "conflict.report_conflicts",
            '[process_detection]\nallow_terminate_if_running = "true"\n': (
                "process_detection.allow_terminate_if_running"
            ),
        }
        for extra, name in cases.items():
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ConfigError, name + " must be true or false"):
                    _parse(extra)

    def test_real_booleans_and_defaults_still_load(self) -> None:
        cfg = _parse("[sync]\ndry_run_default = false\n")
        self.assertFalse(cfg.sync.dry_run_default)
        self.assertTrue(_parse().backup.backup_before_overwrite)


class MissingTargetsTests(unittest.TestCase):
    def test_a_config_without_include_roots_loads_and_is_marked_unlisted(self) -> None:
        # Guardian-only configs are legitimate; `sync` refuses them and
        # `validate` notes it (see app.validate_config_only).
        cfg = _parse()
        self.assertFalse(cfg.targets.listed)
        self.assertEqual(cfg.targets.include_roots, [])

    def test_an_empty_list_is_still_refused(self) -> None:
        with self.assertRaisesRegex(ConfigError, "include_roots"):
            _parse("[targets]\ninclude_roots = []\n")


if __name__ == "__main__":
    unittest.main()
