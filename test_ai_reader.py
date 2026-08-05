"""Unit tests for the snap-to-roster fuzzy matcher in ai_reader.py."""
import unittest

from ai_reader import snap_to_roster

CLIENTS = [
    "Pauline Beasley",
    "Johnnie Jones",
    "Fedrico Wilson",
    "Sandra Easterling",
    "Sandra Hartfield",
    "Ronald Melvin",
]

EMPLOYEES = [
    "Jackson Cassandra",
    "Latonja McLeod",
]


class SnapToRosterTests(unittest.TestCase):
    def test_exact_match_passes_through(self):
        self.assertEqual(
            snap_to_roster("Pauline Beasley", "PCS", CLIENTS),
            ("Pauline Beasley", True),
        )

    def test_close_misspelling_snaps_to_roster(self):
        self.assertEqual(
            snap_to_roster("Pauline Beasly", "PCS", CLIENTS),
            ("Pauline Beasley", True),
        )
        self.assertEqual(
            snap_to_roster("Johnie Jones", "IHR", CLIENTS),
            ("Johnnie Jones", True),
        )
        self.assertEqual(
            snap_to_roster("Federico Wilson", "E&D", CLIENTS),
            ("Fedrico Wilson", True),
        )

    def test_case_is_normalised_to_roster_spelling(self):
        self.assertEqual(
            snap_to_roster("johnnie jones", "Trans", CLIENTS),
            ("Johnnie Jones", True),
        )

    def test_similar_first_names_pick_right_person(self):
        self.assertEqual(
            snap_to_roster("Sandra Esterling", "PCS", CLIENTS),
            ("Sandra Easterling", True),
        )
        self.assertEqual(
            snap_to_roster("Sandra Hartfeild", "PCS", CLIENTS),
            ("Sandra Hartfield", True),
        )

    def test_reversed_word_order_still_matches(self):
        self.assertEqual(
            snap_to_roster("Cassandra Jackson", "TS", EMPLOYEES),
            ("Jackson Cassandra", True),
        )

    def test_unrelated_name_is_left_alone(self):
        name, matched = snap_to_roster("Quincy Adams", "PCS", CLIENTS)
        self.assertEqual(name, "Quincy Adams")
        self.assertFalse(matched)

    def test_empty_name_and_empty_roster(self):
        self.assertEqual(snap_to_roster("", "PCS", CLIENTS), ("", False))
        self.assertEqual(
            snap_to_roster("Pauline Beasley", "", CLIENTS),
            ("Pauline Beasley", True),
        )
        self.assertEqual(
            snap_to_roster("Pauline Beasly", "???", []),
            ("Pauline Beasly", False),
        )


if __name__ == "__main__":
    unittest.main()
