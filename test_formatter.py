"""Unit tests for formatter.py."""
import unittest
from datetime import date

from formatter import build_filename, clean_name, format_dates, sanitize


class TestDateFormatting(unittest.TestCase):
    def test_single_day(self):
        self.assertEqual(format_dates([date(2026, 2, 23)]), "2-23-26")

    def test_multiple_days_same_month(self):
        dates = [date(2026, 2, 23), date(2026, 2, 24), date(2026, 2, 26)]
        self.assertEqual(format_dates(dates), "2-23,24,26-26")

    def test_multiple_months(self):
        dates = [date(2026, 2, 23), date(2026, 2, 24),
                 date(2026, 3, 1),  date(2026, 3, 2)]
        self.assertEqual(format_dates(dates), "2-23,24_3-1,2-26")

    def test_deduplicates_and_sorts(self):
        dates = [date(2026, 2, 24), date(2026, 2, 23), date(2026, 2, 24)]
        self.assertEqual(format_dates(dates), "2-23,24-26")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            format_dates([])

    def test_cross_year_span(self):
        """Bi-weekly TS straddling year-end keeps a -YY per month group."""
        dates = [date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31),
                 date(2026, 1, 1),  date(2026, 1, 2),  date(2026, 1, 5)]
        self.assertEqual(format_dates(dates), "12-29,30,31-25_1-1,2,5-26")


class TestNameCleaning(unittest.TestCase):
    def test_title_case(self):
        self.assertEqual(clean_name("kofi jamar"), "Kofi Jamar")

    def test_extra_whitespace(self):
        self.assertEqual(clean_name("  kofi   JAMAR  "), "Kofi Jamar")


class TestBuildFilename(unittest.TestCase):
    def test_pcs_single_day(self):
        fn = build_filename("Kofi Jamar", "PCS", [date(2026, 2, 23)])
        self.assertEqual(fn, "Kofi Jamar_PCS_2-23-26.pdf")

    def test_pcs_multi_day(self):
        fn = build_filename("Kofi Jamar", "PCS",
                            [date(2026, 2, 23), date(2026, 2, 24), date(2026, 2, 26)])
        self.assertEqual(fn, "Kofi Jamar_PCS_2-23,24,26-26.pdf")

    def test_pcs_multi_month(self):
        fn = build_filename("Kofi Jamar", "PCS",
                            [date(2026, 2, 23), date(2026, 2, 24),
                             date(2026, 3, 1),  date(2026, 3, 2)])
        self.assertEqual(fn, "Kofi Jamar_PCS_2-23,24_3-1,2-26.pdf")

    def test_timesheet(self):
        fn = build_filename("Ama Owusu", "TS", [date(2026, 4, 10), date(2026, 4, 11)])
        self.assertEqual(fn, "Ama Owusu_TS_4-10,11-26.pdf")

    def test_invalid_type(self):
        with self.assertRaises(ValueError):
            build_filename("A B", "WRONG", [date(2026, 1, 1)])

    def test_example_from_uploads_thereza(self):
        """Matches 'Thereza Zanzen_PCS_3-30,31_4-1,2,3-26.pdf' from the samples."""
        fn = build_filename(
            "Thereza Zanzen", "PCS",
            [date(2026, 3, 30), date(2026, 3, 31),
             date(2026, 4, 1),  date(2026, 4, 2), date(2026, 4, 3)],
        )
        self.assertEqual(fn, "Thereza Zanzen_PCS_3-30,31_4-1,2,3-26.pdf")

    def test_example_from_uploads_tiffany(self):
        """Matches 'Tiffany Bolton_TS_3-23,24,25,26,27,30,31_4-1,2,3-26.pdf'."""
        fn = build_filename(
            "Tiffany Bolton", "TS",
            [date(2026, 3, 23), date(2026, 3, 24), date(2026, 3, 25),
             date(2026, 3, 26), date(2026, 3, 27), date(2026, 3, 30), date(2026, 3, 31),
             date(2026, 4, 1),  date(2026, 4, 2),  date(2026, 4, 3)],
        )
        self.assertEqual(fn, "Tiffany Bolton_TS_3-23,24,25,26,27,30,31_4-1,2,3-26.pdf")


class TestSanitize(unittest.TestCase):
    def test_removes_illegal(self):
        self.assertEqual(sanitize("a/b:c*d?.pdf"), "a-b-c-d-.pdf")


if __name__ == "__main__":
    unittest.main()
