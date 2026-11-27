"""Tests for woo-variation-doctor."""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import woo_variation_doctor as wvd

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "examples")

AS_OF = datetime(2026, 11, 18, tzinfo=timezone.utc)

HEADER = ("parent_id,variation_id,attributes,regular_price,"
          "sale_price,sale_start,sale_end,stock,sku,enabled,"
          "backorder\n")


def write_file(content, suffix=".csv"):
    """Write content to a temp file and return its path."""
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=suffix, delete=False, encoding="utf-8")
    handle.write(content)
    handle.close()
    return handle.name


def row(parent_id="P-100", variation_id="V-101",
        attributes="Size:S;Color:Red", regular_price="29.99",
        sale_price="", sale_start="", sale_end="", stock="12",
        sku="TS-S-RED", enabled="yes", backorder="no"):
    """Build one CSV row with the standard columns."""
    return ",".join([parent_id, variation_id, attributes,
                     regular_price, sale_price, sale_start,
                     sale_end, stock, sku, enabled,
                     backorder]) + "\n"


def parse(content, as_of=AS_OF):
    """Parse CSV content and return (variations, findings)."""
    return wvd.parse_csv(write_file(content), as_of)


def audit(content, as_of=AS_OF, **kwargs):
    """Parse CSV content and audit it, returning all findings."""
    variations, findings = parse(content, as_of)
    findings.extend(wvd.audit(variations, as_of, **kwargs))
    return variations, findings


def rules(findings):
    """Return the sorted rule names of a findings list."""
    return sorted(item.rule for item in findings)


def by_rule(findings, rule):
    """Return the findings with one rule name."""
    return [item for item in findings if item.rule == rule]


def run_cli(argv):
    """Run main with argv, capturing stdout, returning (code, text)."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = wvd.main(argv)
    return code, buffer.getvalue()


class TestMoneyParsing(unittest.TestCase):
    """Money field parsing."""

    def test_plain_number(self):
        value, valid, empty = wvd.parse_money("29.99")
        self.assertEqual(value, Decimal("29.99"))
        self.assertTrue(valid)
        self.assertFalse(empty)

    def test_dollar_sign_and_commas(self):
        value, valid, _empty = wvd.parse_money("$1,299.50")
        self.assertEqual(value, Decimal("1299.50"))
        self.assertTrue(valid)

    def test_empty_is_empty(self):
        value, valid, empty = wvd.parse_money("")
        self.assertIsNone(value)
        self.assertTrue(valid)
        self.assertTrue(empty)

    def test_bad_value_is_invalid(self):
        _value, valid, empty = wvd.parse_money("abc")
        self.assertFalse(valid)
        self.assertFalse(empty)

    def test_negative_is_invalid(self):
        _value, valid, _empty = wvd.parse_money("-10.00")
        self.assertFalse(valid)


class TestStockParsing(unittest.TestCase):
    """Stock field parsing."""

    def test_whole_number(self):
        value, valid, empty = wvd.parse_stock("12")
        self.assertEqual(value, 12)
        self.assertTrue(valid)
        self.assertFalse(empty)

    def test_zero(self):
        value, valid, _empty = wvd.parse_stock("0")
        self.assertEqual(value, 0)
        self.assertTrue(valid)

    def test_empty_is_empty(self):
        value, valid, empty = wvd.parse_stock("")
        self.assertIsNone(value)
        self.assertTrue(valid)
        self.assertTrue(empty)

    def test_decimal_is_invalid(self):
        _value, valid, _empty = wvd.parse_stock("2.5")
        self.assertFalse(valid)

    def test_negative_is_invalid(self):
        _value, valid, _empty = wvd.parse_stock("-3")
        self.assertFalse(valid)

    def test_text_is_invalid(self):
        _value, valid, _empty = wvd.parse_stock("many")
        self.assertFalse(valid)


class TestBoolParsing(unittest.TestCase):
    """Yes/no flag parsing."""

    def test_true_values(self):
        for text in ("yes", "true", "1", "y", "YES"):
            self.assertTrue(wvd.parse_bool(text, default=False))

    def test_false_values(self):
        for text in ("no", "false", "0", "n", "NO"):
            self.assertFalse(wvd.parse_bool(text, default=True))

    def test_empty_returns_default(self):
        self.assertTrue(wvd.parse_bool("", default=True))
        self.assertFalse(wvd.parse_bool("", default=False))

    def test_invalid_is_none(self):
        self.assertIsNone(wvd.parse_bool("maybe", default=True))


class TestDateParsing(unittest.TestCase):
    """Date string parsing."""

    def test_iso_date(self):
        self.assertEqual(wvd.parse_date("2026-11-18"),
                         date(2026, 11, 18))

    def test_slash_date(self):
        self.assertEqual(wvd.parse_date("2026/11/18"),
                         date(2026, 11, 18))

    def test_empty_and_bad_are_none(self):
        self.assertIsNone(wvd.parse_date(""))
        self.assertIsNone(wvd.parse_date(None))
        self.assertIsNone(wvd.parse_date("2026-13-01"))


class TestAttributeParsing(unittest.TestCase):
    """Attributes cell parsing."""

    def test_two_pairs(self):
        key, bad = wvd.parse_attributes("Size:M;Color:Red")
        self.assertEqual(key, (("color", "red"), ("size", "m")))
        self.assertEqual(bad, [])

    def test_order_and_case_do_not_matter(self):
        first, _bad = wvd.parse_attributes("Size:M;Color:Red")
        second, _bad = wvd.parse_attributes("color:red;SIZE:m")
        self.assertEqual(first, second)

    def test_empty_cell(self):
        self.assertEqual(wvd.parse_attributes(""), ((), []))
        self.assertEqual(wvd.parse_attributes(None), ((), []))

    def test_bad_entries_reported(self):
        key, bad = wvd.parse_attributes("Size:M;nosuchpair;Color:")
        self.assertEqual(key, (("size", "m"),))
        self.assertEqual(bad, ["nosuchpair", "Color:"])

    def test_spaces_around_pairs(self):
        key, bad = wvd.parse_attributes(" Size:M ; Color:Red ")
        self.assertEqual(key, (("color", "red"), ("size", "m")))
        self.assertEqual(bad, [])


class TestHeaderAndParsing(unittest.TestCase):
    """CSV header handling and row parsing."""

    def test_missing_variation_column_errors(self):
        with self.assertRaises(ValueError) as caught:
            parse("parent_id,attributes\nP-100,Size:S\n")
        self.assertIn("no variation id column",
                      str(caught.exception))

    def test_empty_file_errors(self):
        with self.assertRaises(ValueError):
            parse("")

    def test_bom_header_handled(self):
        variations, findings = parse("\ufeff" + HEADER + row())
        self.assertEqual(len(variations), 1)
        self.assertEqual(findings, [])

    def test_extra_columns_ignored(self):
        content = (HEADER.rstrip("\n") + ",notes\n"
                   + row().rstrip("\n") + ",hello\n")
        variations, findings = parse(content)
        self.assertEqual(len(variations), 1)
        self.assertEqual(findings, [])

    def test_column_aliases(self):
        content = ("parent,variation,regular,sale,quantity\n"
                   + "P-100,V-101,29.99,,12\n")
        variations, findings = parse(content)
        self.assertEqual(len(variations), 1)
        self.assertEqual(variations[0]["regular_price"],
                         Decimal("29.99"))
        self.assertEqual(findings, [])

    def test_missing_optional_columns_skipped(self):
        variations, findings = parse("variation_id\nV-101\n")
        self.assertEqual(len(variations), 1)
        self.assertEqual(findings, [])

    def test_malformed_row_skipped(self):
        variations, findings = parse(HEADER + row()
                                     + "BADROW,111\n")
        self.assertEqual(len(variations), 1)
        self.assertEqual(rules(findings), ["malformed-row"])

    def test_empty_variation_skipped(self):
        variations, findings = parse(HEADER + row()
                                     + row(variation_id=""))
        self.assertEqual(len(variations), 1)
        self.assertEqual(rules(findings), ["empty-variation"])

    def test_blank_lines_skipped(self):
        variations, findings = parse(HEADER + "\n" + row() + "\n")
        self.assertEqual(len(variations), 1)
        self.assertEqual(findings, [])


class TestBadData(unittest.TestCase):
    """Bad data findings."""

    def test_invalid_regular_price(self):
        _vars, findings = parse(HEADER + row(regular_price="abc"))
        self.assertEqual(rules(findings), ["invalid-price"])

    def test_invalid_sale_price(self):
        _vars, findings = parse(HEADER + row(sale_price="free"))
        self.assertEqual(rules(findings), ["invalid-price"])

    def test_negative_price(self):
        _vars, findings = parse(HEADER
                                + row(regular_price="-10.00"))
        self.assertEqual(rules(findings), ["invalid-price"])

    def test_invalid_stock(self):
        _vars, findings = parse(HEADER + row(stock="2.5"))
        self.assertEqual(rules(findings), ["invalid-stock"])

    def test_invalid_sale_date(self):
        _vars, findings = parse(HEADER
                                + row(sale_start="2026-13-01"))
        self.assertEqual(rules(findings), ["invalid-date"])

    def test_invalid_attribute(self):
        _vars, findings = parse(HEADER
                                + row(attributes="SizeM"))
        self.assertEqual(rules(findings), ["invalid-attribute"])

    def test_invalid_enabled_flag(self):
        _vars, findings = parse(HEADER + row(enabled="maybe"))
        self.assertEqual(rules(findings), ["invalid-flag"])

    def test_duplicate_variation(self):
        content = HEADER + row() + row(
            attributes="Size:M", sku="TS-M-RED")
        _vars, findings = parse(content)
        self.assertEqual(rules(findings), ["duplicate-variation"])

    def test_duplicate_sku(self):
        content = HEADER + row() + row(variation_id="V-102",
                                       attributes="Size:M")
        _vars, findings = parse(content)
        self.assertEqual(rules(findings), ["duplicate-sku"])

    def test_duplicate_sku_case_insensitive(self):
        content = HEADER + row() + row(variation_id="V-102",
                                       attributes="Size:M",
                                       sku="ts-s-red")
        _vars, findings = parse(content)
        self.assertEqual(rules(findings), ["duplicate-sku"])

    def test_empty_sku_is_not_a_duplicate(self):
        content = HEADER + row(sku="") + row(
            variation_id="V-102", attributes="Size:M", sku="")
        _vars, findings = parse(content)
        self.assertEqual(findings, [])

    def test_invalid_price_does_not_also_flag_no_price(self):
        _vars, findings = audit(HEADER
                                + row(regular_price="abc"))
        self.assertEqual(rules(findings), ["invalid-price"])


class TestPriceRules(unittest.TestCase):
    """Price and sale rules."""

    def test_clean_variation_has_no_findings(self):
        _vars, findings = audit(HEADER + row())
        self.assertEqual(findings, [])

    def test_no_price_error(self):
        _vars, findings = audit(HEADER + row(regular_price=""))
        self.assertEqual(rules(findings), ["no-price"])

    def test_sale_above_regular_error(self):
        _vars, findings = audit(HEADER + row(
            sale_price="34.99", sale_start="2026-11-01",
            sale_end="2026-11-30"))
        self.assertEqual(rules(findings), ["sale-above-regular"])

    def test_sale_equal_to_regular_is_fine(self):
        _vars, findings = audit(HEADER + row(
            sale_price="29.99", sale_start="2026-11-01",
            sale_end="2026-11-30"))
        self.assertEqual(findings, [])

    def test_sale_no_end_warning(self):
        _vars, findings = audit(HEADER + row(
            sale_price="19.99", sale_start="2026-11-01"))
        self.assertEqual(rules(findings), ["sale-no-end"])

    def test_sale_with_end_is_fine(self):
        _vars, findings = audit(HEADER + row(
            sale_price="19.99", sale_start="2026-11-01",
            sale_end="2026-11-30"))
        self.assertEqual(findings, [])

    def test_expired_sale_active_error(self):
        _vars, findings = audit(HEADER + row(
            sale_price="14.99", sale_start="2026-10-01",
            sale_end="2026-10-31"))
        self.assertEqual(rules(findings), ["expired-sale-active"])

    def test_expired_sale_on_disabled_variation_is_fine(self):
        _vars, findings = audit(HEADER + row(
            sale_price="14.99", sale_start="2026-10-01",
            sale_end="2026-10-31", enabled="no"))
        self.assertEqual(rules(findings),
                         ["stock-but-disabled"])

    def test_sale_ending_today_is_not_expired(self):
        _vars, findings = audit(HEADER + row(
            sale_price="14.99", sale_start="2026-11-01",
            sale_end="2026-11-18"))
        self.assertEqual(findings, [])

    def test_sale_ending_yesterday_is_expired(self):
        _vars, findings = audit(HEADER + row(
            sale_price="14.99", sale_start="2026-11-01",
            sale_end="2026-11-17"))
        self.assertEqual(rules(findings), ["expired-sale-active"])

    def test_as_of_flag_changes_expiry(self):
        _vars, findings = audit(HEADER + row(
            sale_price="14.99", sale_start="2026-10-01",
            sale_end="2026-10-31"),
            as_of=datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertEqual(findings, [])

    def test_sale_start_after_end_error(self):
        _vars, findings = audit(HEADER + row(
            sale_price="9.99", sale_start="2026-11-15",
            sale_end="2026-11-10"))
        self.assertEqual(rules(findings), ["invalid-sale-dates"])

    def test_sale_price_with_no_regular_price(self):
        _vars, findings = audit(HEADER + row(
            regular_price="", sale_price="9.99",
            sale_start="2026-11-01", sale_end="2026-11-30"))
        self.assertEqual(rules(findings), ["no-price"])


class TestStockRules(unittest.TestCase):
    """Stock, enabled, and backorder rules."""

    def test_stock_but_disabled_warning(self):
        _vars, findings = audit(HEADER + row(enabled="no"))
        self.assertEqual(rules(findings), ["stock-but-disabled"])

    def test_disabled_with_zero_stock_is_fine(self):
        _vars, findings = audit(HEADER + row(stock="0",
                                             enabled="no"))
        self.assertEqual(findings, [])

    def test_zero_stock_enabled_warning(self):
        _vars, findings = audit(HEADER + row(stock="0"))
        self.assertEqual(rules(findings), ["zero-stock-enabled"])

    def test_zero_stock_with_backorder_is_fine(self):
        _vars, findings = audit(HEADER + row(stock="0",
                                             backorder="yes"))
        self.assertEqual(findings, [])

    def test_zero_stock_disabled_is_fine(self):
        _vars, findings = audit(HEADER + row(stock="0",
                                             enabled="no"))
        self.assertEqual(findings, [])

    def test_stocked_and_enabled_is_fine(self):
        _vars, findings = audit(HEADER + row(stock="5"))
        self.assertEqual(findings, [])


class TestParentRules(unittest.TestCase):
    """Per-parent rules."""

    def test_duplicate_attributes_error(self):
        content = HEADER + row() + row(
            variation_id="V-102", sku="TS-M-RED")
        _vars, findings = audit(content)
        self.assertEqual(rules(findings), ["duplicate-attributes"])

    def test_same_attributes_different_parent_is_fine(self):
        content = HEADER + row() + row(
            parent_id="P-200", variation_id="V-201",
            sku="HD-S-RED")
        _vars, findings = audit(content)
        self.assertEqual(findings, [])

    def test_attribute_order_does_not_hide_duplicate(self):
        content = HEADER + row() + row(
            variation_id="V-102", attributes="Color:Red;Size:S",
            sku="TS-S-RED2")
        _vars, findings = audit(content)
        self.assertEqual(rules(findings), ["duplicate-attributes"])

    def test_empty_attributes_can_duplicate(self):
        content = HEADER + row(attributes="") + row(
            variation_id="V-102", attributes="", sku="TS-M")
        _vars, findings = audit(content)
        self.assertEqual(rules(findings), ["duplicate-attributes"])

    def test_too_many_variations_warning(self):
        content = HEADER
        for index in range(6):
            content += row(variation_id="V-%d" % (101 + index),
                           attributes="Size:%d" % index,
                           sku="SKU-%d" % index)
        _vars, findings = audit(content, max_variations=5)
        self.assertEqual(rules(findings), ["too-many-variations"])

    def test_exactly_at_max_variations_passes(self):
        content = HEADER
        for index in range(5):
            content += row(variation_id="V-%d" % (101 + index),
                           attributes="Size:%d" % index,
                           sku="SKU-%d" % index)
        _vars, findings = audit(content, max_variations=5)
        self.assertEqual(findings, [])


class TestSummaryAndTables(unittest.TestCase):
    """Summary counts and the per-parent table."""

    def test_sample_summary(self):
        path = os.path.join(EXAMPLES, "variations.csv")
        variations, findings = wvd.parse_csv(path, AS_OF)
        findings.extend(wvd.audit(variations, AS_OF))
        summary = wvd.summarize(variations, findings)
        self.assertEqual(summary["variations"], 23)
        self.assertEqual(summary["parents"], 4)
        self.assertEqual(summary["findings"], 16)
        self.assertEqual(summary["errors"], 11)
        self.assertEqual(summary["warnings"], 5)
        self.assertEqual(summary["on_sale"], 7)
        self.assertEqual(summary["no_price"], 1)
        self.assertEqual(summary["duplicate_skus"], 1)

    def test_sample_per_parent_table(self):
        path = os.path.join(EXAMPLES, "variations.csv")
        variations, findings = wvd.parse_csv(path, AS_OF)
        findings.extend(wvd.audit(variations, AS_OF))
        summary = wvd.summarize(variations, findings)
        table = {entry["parent"]: entry
                 for entry in summary["per_parent"]}
        self.assertEqual(table["P-100"]["variations"], 8)
        self.assertEqual(table["P-100"]["min_price"],
                         Decimal("29.99"))
        self.assertEqual(table["P-100"]["max_price"],
                         Decimal("34.99"))
        self.assertEqual(table["P-100"]["on_sale"], 4)
        self.assertEqual(table["P-200"]["min_price"],
                         Decimal("49.99"))
        self.assertEqual(table["P-200"]["on_sale"], 1)
        self.assertEqual(table["P-400"]["variations"], 5)

    def test_clean_summary(self):
        path = os.path.join(EXAMPLES, "clean.csv")
        variations, findings = wvd.parse_csv(path, AS_OF)
        findings.extend(wvd.audit(variations, AS_OF))
        summary = wvd.summarize(variations, findings)
        self.assertEqual(summary["findings"], 0)
        self.assertEqual(summary["variations"], 3)
        self.assertEqual(summary["parents"], 2)


class TestOutput(unittest.TestCase):
    """Text and JSON output, --check, --strict, --out."""

    def test_text_output_sample(self):
        path = os.path.join(EXAMPLES, "variations.csv")
        code, text = run_cli([path, "--as-of", "2026-11-18"])
        self.assertEqual(code, 0)
        self.assertIn("Variations: 23, parents: 4, findings: 16, "
                      "errors: 11, warnings: 5", text)
        self.assertIn("On sale: 7, variations with no price: 1, "
                      "duplicate SKUs: 1", text)
        self.assertIn("P-100               8      $29.99      "
                      "$34.99       4", text)

    def test_json_output(self):
        path = os.path.join(EXAMPLES, "variations.csv")
        code, text = run_cli([path, "--as-of", "2026-11-18",
                              "--json"])
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["variations"], 23)
        self.assertEqual(len(report["findings"]), 16)
        self.assertEqual(report["errors"], 11)
        self.assertEqual(len(report["per_parent"]), 4)
        self.assertEqual(report["per_parent"][0]["min_price"],
                         "29.99")
        first = report["findings"][0]
        self.assertEqual(set(first), {"subject", "rule",
                                      "severity", "message"})

    def test_clean_text_output(self):
        path = os.path.join(EXAMPLES, "clean.csv")
        code, text = run_cli([path, "--as-of", "2026-11-18"])
        self.assertEqual(code, 0)
        self.assertIn("All variations clean.", text)
        self.assertIn("Variations: 3, parents: 2, findings: 0, "
                      "errors: 0, warnings: 0", text)

    def test_check_exits_1_on_error(self):
        path = os.path.join(EXAMPLES, "variations.csv")
        code, _text = run_cli([path, "--as-of", "2026-11-18",
                               "--check"])
        self.assertEqual(code, 1)

    def test_check_exits_0_on_clean(self):
        path = os.path.join(EXAMPLES, "clean.csv")
        code, _text = run_cli([path, "--as-of", "2026-11-18",
                               "--check"])
        self.assertEqual(code, 0)

    def test_warnings_only_passes_check_fails_strict(self):
        path = os.path.join(EXAMPLES, "warnings-only.csv")
        code, text = run_cli([path, "--as-of", "2026-11-18",
                              "--check"])
        self.assertEqual(code, 0)
        self.assertIn("errors: 0, warnings: 3", text)
        code, _text = run_cli([path, "--as-of", "2026-11-18",
                               "--check", "--strict"])
        self.assertEqual(code, 1)

    def test_as_of_flag_drops_expired_sale(self):
        path = os.path.join(EXAMPLES, "variations.csv")
        code, text = run_cli([path, "--as-of", "2026-10-15",
                              "--check"])
        self.assertEqual(code, 1)
        self.assertIn("findings: 15, errors: 10, warnings: 5",
                      text)

    def test_max_variations_flag_adds_warnings(self):
        path = os.path.join(EXAMPLES, "variations.csv")
        code, text = run_cli([path, "--as-of", "2026-11-18",
                              "--max-variations", "5", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("findings: 18, errors: 11, warnings: 7",
                      text)

    def test_out_writes_file(self):
        path = os.path.join(EXAMPLES, "clean.csv")
        out = write_file("", suffix=".txt")
        code, text = run_cli([path, "--as-of", "2026-11-18",
                              "--out", out])
        self.assertEqual(code, 0)
        self.assertEqual(text, "")
        with open(out, encoding="utf-8") as handle:
            self.assertIn("All variations clean.",
                          handle.read())

    def test_missing_file_exits_2(self):
        code, _text = run_cli(["no-such-file.csv"])
        self.assertEqual(code, 2)

    def test_bad_as_of_exits_2(self):
        path = os.path.join(EXAMPLES, "clean.csv")
        code, _text = run_cli([path, "--as-of", "not-a-date"])
        self.assertEqual(code, 2)

    def test_negative_max_variations_exits_2(self):
        path = os.path.join(EXAMPLES, "clean.csv")
        code, _text = run_cli([path, "--max-variations", "-1"])
        self.assertEqual(code, 2)


class TableOutputTests(unittest.TestCase):
    def run_command(self, path, *flags):
        script = Path(EXAMPLES).parent / "woo_variation_doctor.py"
        return subprocess.run(
            [sys.executable, str(script), str(path), "--as-of",
             "2026-11-18", *map(str, flags)],
            capture_output=True, text=True, timeout=10)

    def test_sample_table_preserves_report_and_check_status(self):
        path = Path(EXAMPLES) / "variations.csv"
        with tempfile.TemporaryDirectory() as directory:
            table = Path(directory) / "table.csv"
            for flags in ((), ("--json",)):
                with self.subTest(flags=flags):
                    baseline = self.run_command(path, "--check", *flags)
                    result = self.run_command(
                        path, "--check", *flags, "--table-out", table)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, baseline.stdout)
                    self.assertEqual(result.stderr, "")
                    with table.open(encoding="utf-8", newline="") as handle:
                        self.assertEqual(list(csv.reader(handle)), [
                            ["parent", "variations", "min_price",
                             "max_price", "on_sale"],
                            ["P-100", "8", "29.99", "34.99", "4"],
                            ["P-200", "6", "49.99", "49.99", "1"],
                            ["P-300", "4", "14.99", "14.99", "1"],
                            ["P-400", "5", "19.99", "19.99", "1"],
                        ])

    def test_table_quotes_parent_and_leaves_missing_prices_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text('variation_id,parent\nV-1,"Café, blue"\n',
                            encoding="utf-8")
            table = Path(directory) / "table.csv"
            report = Path(directory) / "report.json"
            result = self.run_command(
                path, "--table-out", table, "--json", "--out", report)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")),
                             json.loads(self.run_command(path, "--json").stdout))
            with table.open(encoding="utf-8", newline="") as handle:
                self.assertEqual(list(csv.reader(handle))[1:],
                                 [["Café, blue", "1", "", "", "0"]])

    def test_table_rejects_path_collisions_before_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            original = b"variation_id,parent\nV-1,P-100\n"
            path.write_bytes(original)
            alias = Path(directory) / "alias.csv"
            os.link(path, alias)
            out = Path(directory) / "report.csv"
            for table, flags, name in (
                    (path, (), "input"),
                    (alias, (), "input"),
                    (out, ("--out", out), "--out")):
                with self.subTest(table=table):
                    result = self.run_command(
                        path, "--table-out", table, *flags)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("must differ from " + name, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(path.read_bytes(), original)
                    self.assertFalse(out.exists())

    def test_table_write_failure_returns_cli_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(EXAMPLES) / "clean.csv"
            table = Path(directory) / "missing" / "table.csv"
            result = self.run_command(path, "--table-out", table)
            self.assertEqual(result.returncode, 2)
            self.assertIn(f"cannot write {table}", result.stderr)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
