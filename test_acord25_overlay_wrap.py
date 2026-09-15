"""Regression tests for lossless ACORD 25 free-form text rendering.

Run: python -m unittest -v test_acord25_overlay_wrap.py
"""

import datetime as dt
import os
import unittest
from unittest.mock import patch

import fitz

from acord25_2016_overlay import (
    BOX_DESC,
    CarrierIdentityError,
    TextOverflowError,
    _assert_fits,
    _draw_description,
    _fit_size,
    _naic_for,
    _text_width,
    _wrap_block,
    generate_trucking_cert,
)
from cert_generator import CertContent, generate_certificate, render_fallback_certificate


HERE = os.path.dirname(os.path.abspath(__file__))
ACORD25 = os.path.join(HERE, "forms", "acord25_2016.pdf")
ACORD101 = os.path.join(HERE, "forms", "acord101.pdf")

BROKEN_WORDING = (
    "Metro Trans Logistics LLC is included as an Additional Insured with respect "
    "to Covered Auto Liability as required by written contract or agreement, per "
    "the Blanket Additional Insured Endorsement, form with Geico Insurance Company. "
    "Coverage is subject to all policy terms, conditions, and exclusions."
)
FILLER = (
    "Filler clause confirms coverage subject to all policy terms conditions and "
    "exclusions always."
)
# Legacy stamp: the drawer still supports a reserved last line, production passes none.
STAMP = "Issued as a matter of information only."


def content(wording=BROKEN_WORDING, **overrides):
    values = dict(
        cert_number="PRA-TEST",
        issue_date=dt.date(2026, 8, 13),
        producer_block=(
            "Pinnacle Risk Advisors LLC\n22700 Cumberland Pkwy SE STE 410\n"
            "Atlanta, GA 30339\ncerts@pinnacleriskad.com\n(770) 758-3197"
        ),
        insured_name="A Very Long Insured Company Name That Must Wrap LLC",
        insured_address="1234 Extremely Long Industrial Boulevard Suite 999\nAtlanta, GA 30339",
        holder_name="Metro Trans Logistics LLC",
        holder_address="100 Long Holder Avenue Suite 1200\nAtlanta, GA 30339",
        description_of_operations=wording,
        coverages=[],
        data_current_as_of=dt.date(2026, 8, 13),
    )
    values.update(overrides)
    return CertContent(**values)


def spans(page):
    return [
        span
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line.get("spans", [])
    ]


class Acord25WrapTests(unittest.TestCase):
    def test_verified_commercial_auto_entities_override_stale_naic(self):
        self.assertEqual(_naic_for("Progressive Casualty Insurance Co.", "99999"), "24260")
        self.assertEqual(_naic_for("GEICO Marine Insurance Company", "99999"), "37923")

    def test_legacy_company_abbreviation_normalizes(self):
        self.assertEqual(_naic_for("Progressive Mountain Insurance Co", None), "35190")
        self.assertEqual(_naic_for("Canal Insurance Co.", None), "10464")

    def test_brand_only_carrier_is_refused_instead_of_guessed(self):
        for brand in ("GEICO Commercial", "Progressive Commercial"):
            with self.subTest(brand=brand):
                with self.assertRaisesRegex(CarrierIdentityError, "declarations page"):
                    _naic_for(brand, "37923")

    def test_unknown_exact_entity_keeps_provided_naic(self):
        self.assertEqual(_naic_for("Example Specialty Insurance Company", "12345"), "12345")

    def test_measured_broken_wording_wraps_to_two_lines(self):
        self.assertAlmostEqual(_text_width(BROKEN_WORDING, 7.0), 928.7, delta=0.1)
        lines = _wrap_block(BROKEN_WORDING, 7.0, 564.0)
        self.assertEqual(len(lines), 2)
        self.assertAlmostEqual(max(_text_width(x, 7.0) for x in lines), 559.1, delta=0.1)
        _assert_fits(lines, 7.0, 564.0, "broken wording")

    def test_shrink_ladder_reserves_stamp_and_overflows_at_sixteen(self):
        expected = {13: 7.0, 14: 6.5, 15: 6.0, 16: 6.0}
        for repetitions, wanted_size in expected.items():
            wording = " ".join([FILLER] * repetitions)
            size = _fit_size([wording, STAMP], 564.0, 8)
            self.assertEqual(size, wanted_size, repetitions)
            line_count = len(_wrap_block(wording, size, 564.0)) + len(
                _wrap_block(STAMP, size, 564.0)
            )
            self.assertLessEqual(line_count, 8 if repetitions < 16 else line_count)
            if repetitions == 16:
                self.assertGreater(line_count, 8)

    def test_long_tokens_hard_break_losslessly(self):
        tokens = [
            "https://example.com/" + "very-long-path-segment/" * 30,
            "1FUJGLDR8CLBP8834" * 12,
            "certificate-delivery-" * 40 + "@example.com",
        ]
        for original in tokens:
            lines = _wrap_block(original, 7.0, 564.0)
            self.assertEqual("".join(lines), original)
            self.assertTrue(all(_text_width(x, 7.0) <= 564.01 for x in lines))

    def test_overflow_rebuild_preserves_explicit_paragraph_breaks(self):
        block = " ".join([FILLER] * 16) + "\n\nSECOND PARAGRAPH SURVIVES"
        doc = fitz.open()
        page = doc.new_page(width=612, height=792)
        carried = _draw_description(page, [block], STAMP)
        doc.close()
        self.assertIn("\n\nSECOND PARAGRAPH SURVIVES", carried)

    def test_overflow_without_scheduled_units_still_attaches_acord101(self):
        # 22 repetitions overflow the 8-line box even at 6pt with no stamp.
        wording = " ".join([FILLER] * 22)
        pdf = generate_trucking_cert(
            content(wording), ACORD25, ACORD101, include_101=False
        )
        doc = fitz.open(stream=pdf, filetype="pdf")
        self.assertEqual(len(doc), 2)
        self.assertIn("DESCRIPTION OF OPERATIONS (continued):", doc[1].get_text())
        self.assertEqual(
            sum(page.get_text().split().count("Filler") for page in doc), 22
        )
        self.assertNotIn("Coverage data current as of", doc[0].get_text())
        self.assertNotIn("matter of information only", doc[0].get_text())
        doc.close()

    def test_missing_acord101_refuses_instead_of_dropping_text(self):
        wording = " ".join([FILLER] * 22)
        with self.assertRaisesRegex(TextOverflowError, "ACORD101_TEMPLATE_PATH"):
            generate_trucking_cert(
                content(wording), ACORD25, blank_101_path=None, include_101=False
            )

    def test_fallback_keeps_all_44_description_words(self):
        self.assertEqual(len(BROKEN_WORDING.split()), 44)
        pdf = render_fallback_certificate(content())
        doc = fitz.open(stream=pdf, filetype="pdf")
        words = doc[0].get_text().split()
        expected = BROKEN_WORDING.split()
        self.assertTrue(any(words[i:i + len(expected)] == expected
                            for i in range(len(words) - len(expected) + 1)))
        doc.close()

    def test_end_to_end_acord25_spans_stay_inside_right_border(self):
        with patch.dict(os.environ, {"ACORD101_TEMPLATE_PATH": ACORD101}):
            pdf = generate_certificate(content(), template_path=ACORD25)
        doc = fitz.open(stream=pdf, filetype="pdf")
        self.assertTrue(all(span["bbox"][2] <= 594.01 for span in spans(doc[0])))
        self.assertNotIn("Coverage data current as of", doc[0].get_text())
        self.assertNotIn("matter of information only", doc[0].get_text())
        doc.close()

    def test_dated_currency_stamp_is_gone_and_dot_sits_under_address(self):
        pdf = generate_trucking_cert(
            content(usdot="3444355", insured_name="DK FREIGHT MOVERS LLC",
                    insured_address="2780 KEYSTONE AVE, LITHONIA, GA 30058"),
            ACORD25, ACORD101, include_101=False)
        doc = fitz.open(stream=pdf, filetype="pdf")
        text = doc[0].get_text()
        self.assertNotIn("current as of", text)
        self.assertNotIn("Issued as a matter of information only", text)
        by_text = {s["text"].strip(): s for s in spans(doc[0])}
        addr = by_text["2780 KEYSTONE AVE, LITHONIA, GA 30058"]
        dot = by_text["DOT# 3444355"]
        # same left edge, and on the very next line (not floating at y~235)
        self.assertAlmostEqual(addr["bbox"][0], dot["bbox"][0], delta=0.5)
        self.assertLess(dot["bbox"][1] - addr["bbox"][3], 6.0)
        # name/producer/holder text starts below its printed box label
        self.assertGreater(by_text["DK FREIGHT MOVERS LLC"]["bbox"][1], 189.2)
        self.assertGreater(by_text["Pinnacle Risk Advisors LLC"]["bbox"][1], 129.2)
        self.assertGreater(by_text["Metro Trans Logistics LLC"]["bbox"][1], 659.7)
        doc.close()

    def test_guard_runs_before_drawing(self):
        overwide = "W" * 500
        with self.assertRaises(TextOverflowError):
            _assert_fits([overwide], 7.0, BOX_DESC["x_right"] - BOX_DESC["x"], "guard")

    def test_producer_block_is_not_silently_sliced_to_five_lines(self):
        six_lines = "\n".join(f"Producer line {i}" for i in range(1, 7))
        with self.assertRaisesRegex(TextOverflowError, "producer"):
            generate_trucking_cert(
                content(producer_block=six_lines), ACORD25,
                blank_101_path=ACORD101, include_101=False,
            )


if __name__ == "__main__":
    unittest.main()
