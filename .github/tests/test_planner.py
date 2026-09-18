import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

MODULE_PATH = Path(__file__).resolve().parents[2] / "htb_rank_planner.py"
spec = importlib.util.spec_from_file_location("htb_rank_planner", MODULE_PATH)
planner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = planner
assert spec.loader is not None
spec.loader.exec_module(planner)


def action(kind, group, gain, minutes, difficulty=1.0, fb_user=None, fb_root=None):
    return planner.Action(
        kind=kind,
        group=group,
        id=group,
        name=group,
        difficulty=difficulty,
        gain_points=gain,
        flags=1 if kind != "machine_full" else 2,
        est_minutes=minutes,
        fb_user_min=fb_user,
        fb_root_min=fb_root,
    )


class FakeResponse:
    def __init__(self, status_code, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ("" if payload is None else "{}")
        self.headers = headers or {}
        self.url = "https://labs.hackthebox.com/api/v4/test"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeLimiter:
    def __init__(self):
        self.acquired = []
        self.cooldowns = []

    def acquire(self, bucket):
        self.acquired.append(bucket)

    def note_429(self, bucket, retry_after_s):
        self.cooldowns.append((bucket, retry_after_s))


class PlannerTests(unittest.TestCase):
    def test_challenge_solved_alias_auth_user_solve(self):
        self.assertIs(planner._challenge_solved_flag({"authUserSolve": True}), True)
        self.assertIs(planner._challenge_solved_flag({"authUserSolve": False}), False)

    def test_rank_thresholds_match_htb_model(self):
        self.assertEqual(planner._rank_threshold("Script Kiddie"), 5.0)
        self.assertEqual(planner._rank_threshold("Hacker"), 20.0)
        self.assertEqual(planner._rank_threshold("Pro Hacker"), 45.0)
        self.assertEqual(planner._rank_threshold("Elite Hacker"), 70.0)
        self.assertEqual(planner._rank_threshold("Guru"), 90.0)
        self.assertEqual(planner._rank_threshold("Omniscient"), 100.0)

    def test_rank_threshold_text_is_explicit_about_strict_thresholds(self):
        self.assertEqual(planner._rank_threshold_text("Noob", 0.0), ">=0.0%")
        self.assertEqual(planner._rank_threshold_text("Pro Hacker", 45.0), ">45.0%")
        self.assertEqual(planner._rank_threshold_text("Elite Hacker", 70.0), ">70.0%")
        self.assertEqual(planner._rank_threshold_text("Omniscient", 100.0), "=100.0%")

    def test_retained_rank_progress_stays_zero_below_current_rank_floor(self):
        self.assertEqual(planner._retained_rank_progress(50.0, "Elite Hacker", "Guru"), 0.0)

    def test_retained_rank_progress_between_rank_thresholds(self):
        self.assertEqual(planner._retained_rank_progress(80.0, "Elite Hacker", "Guru"), 50.0)

    def test_ownership_formula(self):
        snap = planner.OwnershipSnapshot(
            active_machines_total=20,
            active_challenges_total=100,
            active_user_owns=10,
            active_root_owns=8,
            active_challenge_owns=30,
        )
        expected = (8 + 10 / 2 + 30 / 10) / (20 + 20 / 2 + 100 / 10) * 100
        self.assertAlmostEqual(snap.ownership_percent, expected, places=12)

    def test_difficulty_text_scale_is_monotonic_and_normalized(self):
        self.assertEqual(planner._difficulty_from_value("very easy"), 1.0)
        self.assertEqual(planner._difficulty_from_value("easy"), 2.5)
        self.assertEqual(planner._difficulty_from_value("medium"), 5.0)
        self.assertEqual(planner._difficulty_from_value("hard"), 7.5)
        self.assertEqual(planner._difficulty_from_value("insane"), 9.0)
        self.assertEqual(planner._difficulty_from_value(54), 5.4)
        self.assertEqual(
            planner._extract_user_rated_difficulty({"stars": 4.9, "difficulty": 70}),
            7.0,
        )

    def test_time_parser_common_formats(self):
        self.assertEqual(planner._parse_any_time_to_minutes("1H 30M"), 90.0)
        self.assertEqual(planner._parse_any_time_to_minutes("02:30"), 2.5)
        self.assertEqual(planner._parse_any_time_to_minutes("01:02:30"), 62.5)

    def test_numeric_time_has_explicit_units_without_boundary_jump(self):
        self.assertIsNone(planner._parse_any_time_to_minutes(True))
        self.assertEqual(planner._parse_any_time_to_minutes(120), 120.0)
        self.assertEqual(planner._parse_any_time_to_minutes(121), 121.0)
        self.assertEqual(
            planner._parse_any_time_to_minutes(120, numeric_unit="seconds"),
            2.0,
        )

    def test_time_format_carries_rounded_sixty_minutes(self):
        self.assertEqual(planner._format_minutes(119.6), "2h00m")

    def test_non_finite_difficulty_uses_neutral_fallback(self):
        self.assertEqual(planner._normalize_to_0_10(float("nan")), 5.5)
        self.assertEqual(planner._normalize_to_0_10(float("inf")), 5.5)

    def test_cache_file_is_written_with_restrictive_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = planner.DiskCache(tmp, ttl_seconds=None, enabled=True)
            cache.set("key", {"ok": True})
            path = cache._path("key")
            self.assertEqual(cache.get("key"), {"ok": True})
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_challenge_first_blood_known_fields(self):
        self.assertAlmostEqual(
            planner.extract_challenge_first_blood_minutes(
                {"challenge": {"first_blood_time": "0H 0M 38S"}}
            ),
            38 / 60,
            places=12,
        )
        self.assertEqual(
            planner.extract_challenge_first_blood_minutes(
                {"data": {"blood_difference": "1h 15m"}}
            ),
            75.0,
        )
        self.assertEqual(
            planner.extract_challenge_first_blood_minutes(
                {"data": {"first_blood_seconds": 120}}
            ),
            2.0,
        )

    def test_challenge_first_blood_ignores_unrelated_time_and_ids(self):
        payload = {
            "info": {
                "blood_id": 12,
                "authUserSolveTime": "2m",
                "created_at": "2026-09-18T10:00:00Z",
            }
        }
        self.assertIsNone(planner.extract_challenge_first_blood_minutes(payload))

    def test_active_challenge_index_deduplicates_and_skips_missing_ids(self):
        ids, by_id = planner._index_active_challenges(
            [
                {"id": 1, "name": "first"},
                {"id": 1, "name": "duplicate"},
                {"challenge_id": 2, "name": "second"},
                {"name": "missing"},
            ]
        )
        self.assertEqual(ids, [1, 2])
        self.assertEqual(set(by_id), {"1", "2"})
        self.assertEqual(by_id["1"]["name"], "first")

    def test_machine_active_filter_handles_boolean_shapes(self):
        self.assertTrue(planner._machine_is_active({"active": 1, "retired": 0}))
        self.assertTrue(planner._machine_is_active({"active": "true", "retired": "false"}))
        self.assertFalse(planner._machine_is_active({"active": 0, "retired": 0}))
        self.assertFalse(planner._machine_is_active({"active": 1, "retired": 1}))

    def test_needed_points_crosses_strict_rank_threshold(self):
        snap = planner.OwnershipSnapshot(
            active_machines_total=6,
            active_challenges_total=10,
            active_user_owns=1,
            active_root_owns=4,
            active_challenge_owns=0,
        )
        self.assertEqual(snap.denom_points, 10.0)
        self.assertEqual(snap.numer_points, 4.5)
        self.assertAlmostEqual(planner._needed_points(snap, 45.0, strict=True), 0.1, places=12)
        self.assertEqual(planner._needed_points(snap, 45.0, strict=False), 0.0)

    def test_root_upgrade_uses_incremental_first_blood_gap(self):
        self.assertEqual(planner._estimate_root_upgrade_minutes(6.0, 18.0, 5.0), 12.0)
        self.assertEqual(planner._estimate_root_upgrade_minutes(None, 18.0, 5.0), 18.0)

    def test_max_available_points_uses_best_option_per_group(self):
        groups = [
            [None, action("machine_user", "m1", 0.5, 1), action("machine_full", "m1", 1.5, 2)],
            [None, action("challenge", "c1", 0.1, 1)],
        ]
        self.assertAlmostEqual(planner._max_available_points(groups), 1.6, places=12)

    def test_dp_never_uses_multiple_user_only_machine_solves(self):
        groups = [
            [None, action("machine_user", "m1", 0.5, 1), action("machine_full", "m1", 1.5, 10)],
            [None, action("machine_user", "m2", 0.5, 1), action("machine_full", "m2", 1.5, 10)],
        ]
        result = planner._dp_choose_min_cost(
            groups,
            1.0,
            lambda a: a.est_minutes,
            lambda chosen: (len(chosen),),
        )
        self.assertGreaterEqual(result.total_points, 1.0)
        self.assertLessEqual(sum(a.kind == "machine_user" for a in result.chosen), 1)
        self.assertEqual([a.kind for a in result.chosen], ["machine_full"])

    def test_dp_allows_one_essential_user_only_as_final_step(self):
        groups = [
            [None, action("machine_user", "m1", 0.5, 1), action("machine_full", "m1", 1.5, 100)],
            [None, action("challenge", "c1", 1.0, 5)],
        ]
        result = planner._dp_choose_min_cost(
            groups,
            1.4,
            lambda a: a.est_minutes,
            lambda chosen: (len(chosen),),
        )
        self.assertEqual([a.kind for a in result.chosen], ["challenge", "machine_user"])
        self.assertLess(sum(a.gain_points for a in result.chosen[:-1]), 1.4)
        self.assertGreaterEqual(result.total_points, 1.4)

    def test_easiest_user_only_is_deferred_until_it_can_finish_target(self):
        user = action("machine_user", "m1", 0.5, 10, difficulty=1.0, fb_user=1)
        full = action("machine_full", "m1", 1.5, 30, difficulty=1.0, fb_user=1, fb_root=10)
        challenge = action("challenge", "c1", 1.0, 5, difficulty=0.5, fb_user=1)
        result = planner._choose_easiest_greedy(
            [[None, user, full], [None, challenge]],
            1.4,
        )
        self.assertEqual([a.kind for a in result.chosen], ["challenge", "machine_user"])
        self.assertGreaterEqual(result.total_points, 1.4)

    def test_http_error_keeps_status_for_clean_cli_message(self):
        client = planner.HTBClient(token="x")
        client._tls.session = FakeSession([FakeResponse(401, text='{"error":"Unauthenticated."}')])
        with self.assertRaises(planner.HTBApiError) as ctx:
            client.request("user/info", use_cache=False)
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("rejected the token", planner._friendly_api_error(ctx.exception))

    def test_every_http_retry_reacquires_rate_limits(self):
        limiter = FakeLimiter()
        client = planner.HTBClient(token="x", limiter=limiter)
        client._tls.session = FakeSession(
            [
                FakeResponse(500, text="temporary"),
                FakeResponse(200, payload={"ok": True}, text='{"ok":true}'),
            ]
        )
        with mock.patch.object(planner.time, "sleep", return_value=None), mock.patch.object(
            planner.random, "uniform", return_value=0.0
        ):
            result = client.request("user/info", use_cache=False)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            limiter.acquired,
            ["global", "lists_other", "global", "lists_other"],
        )

    def test_network_retry_reacquires_rate_limits(self):
        limiter = FakeLimiter()
        client = planner.HTBClient(token="x", limiter=limiter)
        client._tls.session = FakeSession(
            [
                requests.ConnectionError("temporary"),
                FakeResponse(200, payload={"ok": True}, text='{"ok":true}'),
            ]
        )
        with mock.patch.object(planner.time, "sleep", return_value=None), mock.patch.object(
            planner.random, "uniform", return_value=0.0
        ):
            result = client.request("user/info", use_cache=False)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(limiter.acquired), 4)

    def test_machine_profile_only_falls_back_on_missing_resource(self):
        client = planner.HTBClient(token="x")
        calls = []

        def fake_request(endpoint, **kwargs):
            calls.append(endpoint)
            if endpoint == "machine/profile/7":
                raise planner.HTBApiError("missing", status_code=404)
            return {"info": {"name": "Example"}}

        client.request = fake_request
        result = client.get_machine_profile_cached(7, "Example")
        self.assertEqual(result["info"]["name"], "Example")
        self.assertEqual(calls, ["machine/profile/7", "machine/profile/Example"])

        def failing_request(endpoint, **kwargs):
            raise planner.HTBApiError("server", status_code=500)

        client.request = failing_request
        with self.assertRaises(planner.HTBApiError):
            client.get_machine_profile_cached(7, "Example")

    def test_detail_error_fatality_only_treats_auth_as_run_fatal(self):
        self.assertTrue(planner._detail_error_is_fatal(planner.HTBApiError("auth", status_code=401)))
        self.assertFalse(planner._detail_error_is_fatal(planner.HTBApiError("server", status_code=500)))
        self.assertFalse(planner._detail_error_is_fatal(planner.HTBApiError("rate", status_code=429)))

    def test_challenge_detail_does_not_hide_server_errors(self):
        client = planner.HTBClient(token="x")

        def forbidden(endpoint, **kwargs):
            raise planner.HTBApiError("no access", status_code=403)

        client.request = forbidden
        self.assertIsNone(client.get_challenge_info_cached(1))

        def server_error(endpoint, **kwargs):
            raise planner.HTBApiError("server", status_code=500)

        client.request = server_error
        with self.assertRaises(planner.HTBApiError):
            client.get_challenge_info_cached(1)


if __name__ == "__main__":
    unittest.main()
