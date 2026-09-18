import importlib.util
import sys
import unittest
from pathlib import Path

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

    def test_time_parser_common_formats(self):
        self.assertEqual(planner._parse_any_time_to_minutes("1H 30M"), 90.0)
        self.assertEqual(planner._parse_any_time_to_minutes("02:30"), 2.5)
        self.assertEqual(planner._parse_any_time_to_minutes("01:02:30"), 62.5)

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


if __name__ == "__main__":
    unittest.main()
