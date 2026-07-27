"""
Tests for polysignals.core.cohorts — pure function coverage only.
No network calls. No DB.

The invariant under test is disjointness. If a market can land in both
arms of the counter-test, the comparison is not between two populations
and the result means nothing — and because markets.market_id is a PRIMARY
KEY written with INSERT OR IGNORE, the failure is silent. These tests are
the only place that failure gets caught.

Run from the repo root: python -m pytest tests/ -v
"""

import unittest

from polysignals.core import cohorts


class _PatchedCohorts(unittest.TestCase):
    """Base class swapping in a small, predictable cohort config."""

    COHORTS = {
        "alpha": ["oscar", "emmy"],
        "beta": ["nba", "super bowl"],
    }

    def setUp(self):
        self._original = cohorts.COHORTS
        cohorts.COHORTS = self.COHORTS

    def tearDown(self):
        cohorts.COHORTS = self._original


class TestMatches(unittest.TestCase):
    def test_substring_match_is_case_insensitive(self):
        self.assertTrue(cohorts.matches("Who wins the OSCAR?", ["oscar"]))
        self.assertTrue(cohorts.matches("oscar night", ["oscar"]))

    def test_no_match(self):
        self.assertFalse(cohorts.matches("Will it rain tomorrow?", ["oscar"]))

    def test_empty_question_is_safe(self):
        self.assertFalse(cohorts.matches("", ["oscar"]))
        self.assertFalse(cohorts.matches(None, ["oscar"]))

    def test_empty_keywords_never_match(self):
        self.assertFalse(cohorts.matches("anything at all", []))


class TestClassify(_PatchedCohorts):
    def test_single_match_classifies(self):
        self.assertEqual(cohorts.classify("Who wins Best Oscar?"), "alpha")
        self.assertEqual(cohorts.classify("Will the NBA season start?"), "beta")

    def test_no_match_returns_none(self):
        self.assertIsNone(cohorts.classify("Will it rain in Denver?"))

    def test_multiple_match_returns_none(self):
        # The invariant: overlap is excluded from BOTH cohorts, never
        # assigned arbitrarily to whichever list was checked first.
        question = "Will the Super Bowl halftime show win an Emmy?"
        self.assertEqual(
            sorted(cohorts.matching_cohorts(question)), ["alpha", "beta"]
        )
        self.assertIsNone(cohorts.classify(question))

    def test_is_in_cohort_respects_exclusivity(self):
        question = "Will the Super Bowl halftime show win an Emmy?"
        self.assertFalse(cohorts.is_in_cohort(question, "alpha"))
        self.assertFalse(cohorts.is_in_cohort(question, "beta"))

    def test_is_in_cohort_positive(self):
        self.assertTrue(cohorts.is_in_cohort("Best Oscar winner?", "alpha"))
        self.assertFalse(cohorts.is_in_cohort("Best Oscar winner?", "beta"))


class TestKeywordsFor(_PatchedCohorts):
    def test_known_cohort(self):
        self.assertEqual(cohorts.keywords_for("alpha"), ["oscar", "emmy"])

    def test_unknown_cohort_raises(self):
        # Returning [] instead would silently match nothing and look like
        # a data problem rather than a typo.
        with self.assertRaises(cohorts.UnknownCohortError):
            cohorts.keywords_for("committe")  # deliberate misspelling

    def test_is_in_cohort_validates_name_even_when_false(self):
        with self.assertRaises(cohorts.UnknownCohortError):
            cohorts.is_in_cohort("Will it rain?", "nonexistent")


class TestCohortReport(_PatchedCohorts):
    def test_counts_split_correctly(self):
        questions = [
            "Who wins the Oscar?",                     # alpha
            "Best Emmy drama?",                        # alpha
            "Will the NBA finals go 7 games?",         # beta
            "Super Bowl winner?",                      # beta
            "Will the Super Bowl ad win an Emmy?",     # both -> ambiguous
            "Will it rain in Denver?",                 # neither
        ]
        report = cohorts.cohort_report(questions)

        self.assertEqual(report["alpha"], 2)
        self.assertEqual(report["beta"], 2)
        self.assertEqual(report["_ambiguous"], 1)
        self.assertEqual(report["_unmatched"], 1)

    def test_report_covers_every_configured_cohort(self):
        report = cohorts.cohort_report([])
        for name in cohorts.available():
            self.assertIn(name, report)
        self.assertEqual(report["_ambiguous"], 0)
        self.assertEqual(report["_unmatched"], 0)

    def test_ambiguous_questions_reports_which_cohorts_collided(self):
        questions = ["Will the Super Bowl ad win an Emmy?"]
        found = cohorts.ambiguous_questions(questions)
        self.assertEqual(len(found), 1)
        question, hits = found[0]
        self.assertEqual(sorted(hits), ["alpha", "beta"])

    def test_ambiguous_questions_respects_limit(self):
        questions = ["Super Bowl Emmy"] * 50
        self.assertEqual(len(cohorts.ambiguous_questions(questions, limit=3)), 3)


class TestRealConfiguration(unittest.TestCase):
    """
    Sanity checks against the ACTUAL configured cohorts, not a fixture.

    These encode the specific collisions worth knowing about: the
    committee list contains "hall of fame" and "trophy", both of which are
    sports-adjacent, so the real lists are exactly the case where
    exclusivity matters.
    """

    def test_both_cohorts_are_configured(self):
        self.assertIn("committee", cohorts.available())
        self.assertIn("sports", cohorts.available())

    def test_clear_committee_questions(self):
        for question in (
            "Will Oppenheimer win Best Picture?",
            "Who will win the Nobel Peace Prize?",
        ):
            with self.subTest(question=question):
                self.assertEqual(cohorts.classify(question), "committee")

    def test_clear_sports_questions(self):
        for question in (
            "Will the Lakers make the playoffs?",
            "Who wins the Premier League?",
        ):
            with self.subTest(question=question):
                self.assertEqual(cohorts.classify(question), "sports")

    def test_known_collision_is_excluded_not_misassigned(self):
        # "super bowl" (sports) + "trophy" (committee). Landing this in
        # either arm would put a sports outcome in the insider-plausible
        # cohort, or an award in the control.
        question = "Who lifts the Super Bowl trophy?"
        self.assertEqual(
            sorted(cohorts.matching_cohorts(question)), ["committee", "sports"]
        )
        self.assertIsNone(cohorts.classify(question))

    def test_hall_of_fame_stays_committee(self):
        # Sports-adjacent but panel-decided, and the sports list is
        # deliberately narrow enough not to claim it.
        question = "Will Ichiro be inducted into the Hall of Fame?"
        self.assertEqual(cohorts.classify(question), "committee")

    def test_politics_is_in_neither_cohort(self):
        # The control arm must not absorb markets where insider
        # information IS plausible. " vs " was excluded from the sports
        # keywords for exactly this reason.
        for question in (
            "Trump vs Biden: who wins the 2024 election?",
            "Will the Fed cut rates in March?",
        ):
            with self.subTest(question=question):
                self.assertIsNone(cohorts.classify(question))


if __name__ == "__main__":
    unittest.main()
