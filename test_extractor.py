"""Sanity tests for the Tesseract extractor."""
import unittest
from datetime import date

from extractor import analyze, detect_type, extract_dates, extract_name


class TestDetectType(unittest.TestCase):
    def test_pcs(self):
        self.assertEqual(detect_type("PERSONAL CARE SERVICES LOG"), "PCS")

    def test_pcs_activity_sheet(self):
        self.assertEqual(detect_type("PCS Activity Sheet"), "PCS")

    def test_pcs_notes(self):
        self.assertEqual(detect_type("PCS Notes and Activity Sheet"), "PCS")

    def test_ihr(self):
        self.assertEqual(detect_type("In-Home Respite authorization"), "IHR")

    def test_trans(self):
        self.assertEqual(detect_type("Transportation log for April"), "Trans")

    def test_ts(self):
        self.assertEqual(detect_type("Employee Timesheet - Pay Period"), "TS")

    def test_ts_biweekly(self):
        self.assertEqual(detect_type("Employee Bi-Weekly Time Entry and Payroll"), "TS")

    def test_none(self):
        self.assertIsNone(detect_type("Just some random text"))


class TestExtractName(unittest.TestCase):
    def test_labelled(self):
        text = "Client Name: Kofi Jamar\nDate of service: 2/23/26"
        self.assertEqual(extract_name(text, "PCS"), "Kofi Jamar")

    def test_employee_label_for_ts(self):
        text = "Employee Name: Ama Owusu\nClient: Someone Else"
        self.assertEqual(extract_name(text, "TS"), "Ama Owusu")


class TestExtractDates(unittest.TestCase):
    def test_numeric(self):
        dates = extract_dates("Service dates: 2/23/26, 2/24/26, 2/26/26")
        self.assertEqual(dates, [date(2026, 2, 23), date(2026, 2, 24), date(2026, 2, 26)])

    def test_text(self):
        dates = extract_dates("Dated: February 23, 2026 and March 1, 2026")
        self.assertEqual(dates, [date(2026, 2, 23), date(2026, 3, 1)])


class TestAnalyze(unittest.TestCase):
    def test_end_to_end(self):
        text = """
        PERSONAL CARE SERVICES TIMESHEET
        Client Name: Kofi Jamar
        Dates of service: 2/23/26, 2/24/26, 2/26/26
        """
        label = analyze(text, source_path="/tmp/foo.pdf")
        self.assertEqual(label.file_type, "PCS")
        self.assertEqual(label.full_name, "Kofi Jamar")
        self.assertEqual(label.filename(), "Kofi Jamar_PCS_2-23,24,26-26.pdf")


if __name__ == "__main__":
    unittest.main()
