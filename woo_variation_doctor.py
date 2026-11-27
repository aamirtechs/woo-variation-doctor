"""WooCommerce product variations audit CLI.

Reads a WooCommerce product variations CSV export (one row
per variation: parent id, variation id, attributes,
regular price, sale price, sale dates, stock, SKU, enabled
flag, backorder flag) and audits it for the variation
problems that silently remove options from the storefront:
variations with no price, sale prices above the regular
price, sale prices with no end date, expired sale dates
still active, duplicate attribute combinations on one
parent, duplicate SKUs across variations, variations with
stock but disabled, enabled variations with zero stock and
no backorder, and bad data such as malformed rows, invalid
prices, negative stock, and bad sale dates. It also prints
a per-parent price range table, so a store owner can see
at a glance what each product actually sells for.

WooCommerce requires variations to have prices to appear
as purchasable choices, so a blank price on one size
silently removes that option from the storefront, and sale
pricing across hundreds of variations is a documented
manual slog because the bulk editor operates at product
level and skips variations. The product level checks in
woo-product-doctor cover missing variation prices as one
check among many, and bulk editors cover parts of this
live, but no free offline CLI audits the variations
export itself.

Each problem is a finding with a variation (or a parent,
or "catalog" for catalog level findings), a rule name, a
severity (error or warning), and a message. Errors are
variations that cannot sell correctly or are recorded
wrong; warnings are patterns worth investigating.
--check exits 1 when any error is found, and --strict
also fails on warnings.

Money uses Decimal math, so price ranges add up exactly.
A bad row is reported as a finding and the row is still
audited on the fields that did parse, so one bad row never
crashes the run.

Zero dependencies, Python 3.9 or newer, standard library only.

Usage:
    python3 woo_variation_doctor.py examples/variations.csv
    python3 woo_variation_doctor.py variations.csv --json --check
    python3 woo_variation_doctor.py variations.csv --as-of 2026-11-18 --max-variations 20
"""

import argparse
import csv
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

__version__ = "1.0.0"

DEFAULT_MAX_VARIATIONS = 50

CENT = Decimal("0.01")


class Finding:
    """One audit finding for one variation, parent, or the catalog."""

    def __init__(self, subject, rule, severity, message):
        self.subject = subject
        self.rule = rule
        self.severity = severity
        self.message = message

    def as_dict(self):
        """Return the finding as a plain dict."""
        return {"subject": self.subject, "rule": self.rule,
                "severity": self.severity, "message": self.message}

    def __str__(self):
        return "%s [%s] %s: %s" % (self.severity.upper(), self.rule,
                                   self.subject, self.message)


def parse_money(text):
    """Parse a money field, returning (value, is_valid, was_empty).

    Accepts a plain number with an optional dollar sign and
    thousands commas. An empty field means no price was
    recorded. A value that is not a number, or a negative
    number, is invalid.
    """
    if text is None:
        return None, True, True
    raw = text.strip().replace("$", "").replace(",", "")
    if not raw:
        return None, True, True
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None, False, False
    if value < 0:
        return None, False, False
    return value, True, False


def parse_stock(text):
    """Parse a stock field, returning (value, is_valid, was_empty).

    Stock must be a whole number. An empty field means no
    stock was recorded. A value that is not a whole number,
    or a negative number, is invalid.
    """
    if text is None:
        return None, True, True
    raw = text.strip().replace(",", "")
    if not raw:
        return None, True, True
    try:
        value = int(raw)
    except ValueError:
        return None, False, False
    if str(value) != raw.lstrip("+") and raw.lstrip("+") != str(value):
        try:
            decimal_value = Decimal(raw)
        except InvalidOperation:
            return None, False, False
        if decimal_value != int(decimal_value):
            return None, False, False
        value = int(decimal_value)
    if value < 0:
        return None, False, False
    return value, True, False


def parse_bool(text, default=None):
    """Parse a yes/no flag, returning True, False, or default.

    Accepts yes, true, 1, and y as True, and no, false, 0,
    and n as False. An empty value returns the default, and
    any other value returns None, so the caller can report
    it as invalid.
    """
    if text is None:
        return default
    raw = text.strip().lower()
    if raw == "":
        return default
    if raw in ("yes", "true", "1", "y", "publish", "active"):
        return True
    if raw in ("no", "false", "0", "n", "draft", "inactive"):
        return False
    return None


def parse_date(text):
    """Parse a date string into a date, or None.

    Accepts ISO dates (2026-11-18), slash dates, and ISO
    datetimes. Returns None for an empty string or a value
    that matches no known form.
    """
    if text is None:
        return None
    raw = text.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def find_column(header, names):
    """Return the first of names present in header, or None."""
    for name in names:
        if name in header:
            return name
    return None


def fmt_money(value):
    """Format a Decimal amount as dollars and cents, or n/a."""
    if value is None:
        return "n/a"
    return "$%s" % format(value.quantize(CENT), ",.2f")


def parse_attributes(text):
    """Parse an attributes cell into a normalized key.

    The cell holds semicolon separated key:value pairs, for
    example "Size:M;Color:Red". Returns (key, bad_entries).
    The key is a tuple of sorted (name, value) pairs with
    both sides lowercased and stripped, so "Color:Red;Size:M"
    and "size:m;color:red" are the same combination. A pair
    with no colon, an empty name, or an empty value is bad
    and is reported in bad_entries instead of the key.
    """
    bad = []
    if text is None:
        return (), bad
    raw = text.strip()
    if not raw:
        return (), bad
    pairs = []
    for part in raw.split(";"):
        entry = part.strip()
        if not entry:
            continue
        if ":" not in entry:
            bad.append(entry)
            continue
        name, value = entry.split(":", 1)
        name = name.strip().lower()
        value = value.strip().lower()
        if not name or not value:
            bad.append(entry)
            continue
        pairs.append((name, value))
    return tuple(sorted(pairs)), bad


def parse_csv(path, as_of):
    """Parse a WooCommerce variations CSV export.

    Returns (variations, findings). Each variation is a dict
    with the parent id, variation id, normalized attribute
    key, regular and sale prices, sale dates, stock, SKU,
    enabled flag, and backorder flag. Rows with the wrong
    number of fields, or with no variation id, are reported
    as findings and skipped, never a crash. A UTF-8 BOM
    header is handled. Extra columns are ignored.
    """
    findings = []
    variations = []
    try:
        handle = open(path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError("cannot read %s: %s" % (path, exc))
    with handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError("%s is empty, no header row found" % path)
        except csv.Error as exc:
            raise ValueError("%s is not a readable CSV file: %s"
                             % (path, exc))
        header = [name.strip() for name in header]
        parent_column = find_column(
            header, ("parent_id", "parent", "parent_product_id",
                     "product_id"))
        variation_column = find_column(
            header, ("variation_id", "variation", "id"))
        if variation_column is None:
            raise ValueError(
                "%s has no variation id column (header is: %s)"
                % (path, ", ".join(header)))
        attributes_column = find_column(
            header, ("attributes", "attribute_values", "options",
                     "attribute"))
        regular_column = find_column(
            header, ("regular_price", "regular", "price"))
        sale_column = find_column(header, ("sale_price", "sale"))
        sale_start_column = find_column(
            header, ("sale_start", "sale_start_date", "sale_from"))
        sale_end_column = find_column(
            header, ("sale_end", "sale_end_date", "sale_to",
                     "sale_until"))
        stock_column = find_column(
            header, ("stock", "stock_quantity", "quantity",
                     "stock_qty"))
        sku_column = find_column(header, ("sku",))
        enabled_column = find_column(
            header, ("enabled", "active", "status"))
        backorder_column = find_column(
            header, ("backorder", "backorders",
                     "allow_backorders", "backordered"))
        seen_variations = set()
        seen_skus = set()
        for line_number, row in enumerate(reader, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            if len(row) != len(header):
                findings.append(Finding(
                    "row %d" % line_number, "malformed-row", "error",
                    "row has %d fields but the header has %d, "
                    "so the row was skipped" % (len(row), len(header))))
                continue
            cells = dict(zip(header, (cell.strip() for cell in row)))
            variation_id = cells[variation_column]
            if not variation_id:
                findings.append(Finding(
                    "row %d" % line_number, "empty-variation",
                    "error", "row has no variation id, so the "
                    "row was skipped"))
                continue
            key = variation_id.lower()
            if key in seen_variations:
                findings.append(Finding(
                    variation_id, "duplicate-variation", "error",
                    "variation id appears more than once, so "
                    "the rows may be the same variation "
                    "exported twice"))
            else:
                seen_variations.add(key)
            variation = {"parent_id": "",
                         "variation_id": variation_id,
                         "attributes": (),
                         "attributes_raw": "",
                         "regular_price": None,
                         "regular_empty": True,
                         "sale_price": None,
                         "sale_empty": True,
                         "sale_start": None,
                         "sale_start_raw": "",
                         "sale_end": None,
                         "sale_end_raw": "",
                         "stock": None,
                         "stock_empty": True,
                         "sku": "",
                         "enabled": True,
                         "backorder": False,
                         "line": line_number}
            if parent_column is not None:
                variation["parent_id"] = cells[parent_column]
            if attributes_column is not None:
                raw = cells[attributes_column]
                variation["attributes_raw"] = raw
                parsed, bad = parse_attributes(raw)
                variation["attributes"] = parsed
                for entry in bad:
                    findings.append(Finding(
                        variation_id, "invalid-attribute", "error",
                        "attribute entry %r is not a name:value "
                        "pair (use Size:M)" % entry))
            if regular_column is not None:
                value, valid, empty = parse_money(
                    cells[regular_column])
                variation["regular_empty"] = empty
                if not valid:
                    findings.append(Finding(
                        variation_id, "invalid-price", "error",
                        "regular_price %r is not a valid "
                        "non-negative price"
                        % cells[regular_column]))
                else:
                    variation["regular_price"] = value
            if sale_column is not None:
                value, valid, empty = parse_money(
                    cells[sale_column])
                variation["sale_empty"] = empty
                if not valid:
                    findings.append(Finding(
                        variation_id, "invalid-price", "error",
                        "sale_price %r is not a valid "
                        "non-negative price"
                        % cells[sale_column]))
                else:
                    variation["sale_price"] = value
            if sale_start_column is not None:
                raw = cells[sale_start_column]
                variation["sale_start_raw"] = raw
                if raw:
                    parsed = parse_date(raw)
                    if parsed is None:
                        findings.append(Finding(
                            variation_id, "invalid-date", "error",
                            "sale_start %r is not a readable date "
                            "(use 2026-11-18)" % raw))
                    else:
                        variation["sale_start"] = parsed
            if sale_end_column is not None:
                raw = cells[sale_end_column]
                variation["sale_end_raw"] = raw
                if raw:
                    parsed = parse_date(raw)
                    if parsed is None:
                        findings.append(Finding(
                            variation_id, "invalid-date", "error",
                            "sale_end %r is not a readable date "
                            "(use 2026-11-18)" % raw))
                    else:
                        variation["sale_end"] = parsed
            if stock_column is not None:
                value, valid, empty = parse_stock(
                    cells[stock_column])
                variation["stock_empty"] = empty
                if not valid:
                    findings.append(Finding(
                        variation_id, "invalid-stock", "error",
                        "stock %r is not a valid non-negative "
                        "whole number" % cells[stock_column]))
                else:
                    variation["stock"] = value
            if sku_column is not None:
                sku = cells[sku_column]
                variation["sku"] = sku
                if sku:
                    sku_key = sku.lower()
                    if sku_key in seen_skus:
                        findings.append(Finding(
                            variation_id, "duplicate-sku", "error",
                            "SKU %s is already used by another "
                            "variation, so orders may pick the "
                            "wrong variation" % sku))
                    else:
                        seen_skus.add(sku_key)
            if enabled_column is not None:
                raw = cells[enabled_column]
                value = parse_bool(raw, default=True)
                if value is None:
                    findings.append(Finding(
                        variation_id, "invalid-flag", "error",
                        "enabled %r is not a yes or no value"
                        % raw))
                else:
                    variation["enabled"] = value
            if backorder_column is not None:
                raw = cells[backorder_column]
                value = parse_bool(raw, default=False)
                if value is None:
                    findings.append(Finding(
                        variation_id, "invalid-flag", "error",
                        "backorder %r is not a yes or no value"
                        % raw))
                else:
                    variation["backorder"] = value
            variations.append(variation)
    return variations, findings


def audit(variations, as_of,
          max_variations=DEFAULT_MAX_VARIATIONS):
    """Audit parsed variations and return a list of findings.

    Variation level rules run first, then the per-parent
    rules (duplicate attribute combinations on one parent,
    parents with too many variations) run over the parents
    found in the file.
    """
    findings = []
    today = as_of.date()
    parents = {}
    for variation in variations:
        variation_id = variation["variation_id"]
        parent_id = variation["parent_id"]
        regular = variation["regular_price"]
        sale = variation["sale_price"]
        if regular is None and not variation["regular_empty"]:
            pass
        elif regular is None:
            findings.append(Finding(
                variation_id, "no-price", "error",
                "variation has no regular price, so it is not "
                "purchasable even when the parent is published"))
        if sale is not None and regular is not None \
                and sale > regular:
            findings.append(Finding(
                variation_id, "sale-above-regular", "error",
                "sale price %s is above the regular price %s"
                % (fmt_money(sale), fmt_money(regular))))
        if sale is not None and variation["sale_end"] is None \
                and variation["sale_end_raw"] == "":
            findings.append(Finding(
                variation_id, "sale-no-end", "warning",
                "sale price %s has no end date, so the sale "
                "runs forever unless someone removes it"
                % fmt_money(sale)))
        if variation["sale_start"] is not None \
                and variation["sale_end"] is not None \
                and variation["sale_start"] > variation["sale_end"]:
            findings.append(Finding(
                variation_id, "invalid-sale-dates", "error",
                "sale start %s is after sale end %s"
                % (variation["sale_start"].isoformat(),
                   variation["sale_end"].isoformat())))
        elif sale is not None and variation["sale_end"] is not None \
                and variation["sale_end"] < today \
                and variation["enabled"]:
            findings.append(Finding(
                variation_id, "expired-sale-active", "error",
                "sale ended %s but the sale price %s is still "
                "set and the variation is still enabled"
                % (variation["sale_end"].isoformat(),
                   fmt_money(sale))))
        if variation["stock"] is not None \
                and variation["stock"] > 0 \
                and not variation["enabled"]:
            findings.append(Finding(
                variation_id, "stock-but-disabled", "warning",
                "variation has %d in stock but is disabled, so "
                "the stock cannot sell" % variation["stock"]))
        if variation["stock"] is not None \
                and variation["stock"] == 0 \
                and variation["enabled"] \
                and not variation["backorder"]:
            findings.append(Finding(
                variation_id, "zero-stock-enabled", "warning",
                "variation is enabled with zero stock and no "
                "backorder, so customers see an option they "
                "cannot buy"))
        if parent_id:
            entry = parents.setdefault(parent_id, {
                "variations": 0, "attributes": {}})
            entry["variations"] += 1
            key = variation["attributes"]
            if key in entry["attributes"]:
                findings.append(Finding(
                    variation_id, "duplicate-attributes", "error",
                    "attribute combination %s is already used "
                    "by variation %s on parent %s, so the two "
                    "variations are the same option"
                    % (variation["attributes_raw"],
                       entry["attributes"][key], parent_id)))
            else:
                entry["attributes"][key] = variation_id
    for parent_id in sorted(parents):
        entry = parents[parent_id]
        if entry["variations"] > max_variations:
            findings.append(Finding(
                parent_id, "too-many-variations", "warning",
                "parent has %d variations, above the limit of "
                "%d, so the product may be too complex to shop"
                % (entry["variations"], max_variations)))
    return findings


def summarize(variations, findings):
    """Return the summary counts and the per-parent table."""
    errors = sum(1 for item in findings if item.severity == "error")
    warnings = sum(1 for item in findings
                   if item.severity == "warning")
    on_sale = sum(1 for item in variations
                  if item["sale_price"] is not None)
    no_price = sum(1 for item in findings
                   if item.rule == "no-price")
    duplicate_skus = sum(1 for item in findings
                         if item.rule == "duplicate-sku")
    parent_map = {}
    for variation in variations:
        parent_id = variation["parent_id"] or "(no parent)"
        entry = parent_map.setdefault(parent_id, {
            "parent": parent_id, "variations": 0,
            "prices": [], "on_sale": 0})
        entry["variations"] += 1
        if variation["regular_price"] is not None:
            entry["prices"].append(variation["regular_price"])
        if variation["sale_price"] is not None:
            entry["on_sale"] += 1
    per_parent = []
    for entry in sorted(parent_map.values(),
                        key=lambda item: item["parent"]):
        prices = entry["prices"]
        per_parent.append({
            "parent": entry["parent"],
            "variations": entry["variations"],
            "min_price": min(prices) if prices else None,
            "max_price": max(prices) if prices else None,
            "on_sale": entry["on_sale"]})
    return {"variations": len(variations),
            "parents": len(parent_map),
            "findings": len(findings),
            "errors": errors,
            "warnings": warnings,
            "on_sale": on_sale,
            "no_price": no_price,
            "duplicate_skus": duplicate_skus,
            "per_parent": per_parent}


def render_text(variations, findings, summary):
    """Render the text report."""
    lines = [str(item) for item in findings]
    lines.append("")
    lines.append("Variations: %(variations)d, parents: "
                 "%(parents)d, findings: %(findings)d, errors: "
                 "%(errors)d, warnings: %(warnings)d" % summary)
    lines.append("On sale: %(on_sale)d, variations with no price: "
                 "%(no_price)d, duplicate SKUs: "
                 "%(duplicate_skus)d" % summary)
    best = None
    for variation in variations:
        regular = variation["regular_price"]
        sale = variation["sale_price"]
        if regular is None or sale is None:
            continue
        discount = regular - sale
        if discount <= 0:
            continue
        if best is None or discount > best[0]:
            best = (discount, variation)
    if best is not None:
        discount, variation = best
        lines.append("Largest discount: %s regular %s, sale %s, "
                     "discount %s"
                     % (variation["variation_id"],
                        fmt_money(variation["regular_price"]),
                        fmt_money(variation["sale_price"]),
                        fmt_money(discount)))
    lines.append("")
    lines.append("Per-parent summary:")
    lines.append("%-10s %10s %11s %11s %7s"
                 % ("parent", "variations", "min price",
                    "max price", "on sale"))
    for entry in summary["per_parent"]:
        lines.append("%-10s %10d %11s %11s %7d"
                     % (entry["parent"], entry["variations"],
                        fmt_money(entry["min_price"]),
                        fmt_money(entry["max_price"]),
                        entry["on_sale"]))
    if summary["findings"] == 0:
        lines.append("")
        lines.append("All variations clean.")
    return "\n".join(lines) + "\n"


def render_json(variations, findings, summary):
    """Render the JSON report."""
    report = dict(summary)
    for entry in report["per_parent"]:
        for key in ("min_price", "max_price"):
            value = entry[key]
            entry[key] = str(value) if value is not None else None
    report["findings"] = [item.as_dict() for item in findings]
    return json.dumps(report, indent=2) + "\n"


def parse_as_of(text):
    """Parse an --as-of date into an aware datetime, or None."""
    if text is None:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_parser():
    """Build the command line parser."""
    parser = argparse.ArgumentParser(
        prog="woo-variation-doctor",
        description="Audit a WooCommerce product variations "
                    "CSV export for unpriced, mispriced, and "
                    "duplicated variations.")
    parser.add_argument("csv_file",
                        help="product variations CSV to audit")
    parser.add_argument("--max-variations", type=int,
                        default=DEFAULT_MAX_VARIATIONS,
                        help="a parent with more variations than "
                        "this is flagged (default: 50)")
    parser.add_argument("--as-of", default=None,
                        help="date to compare sale dates against, "
                        "for example 2026-11-18 (default: now)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of text")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if any error is found")
    parser.add_argument("--strict", action="store_true",
                        help="with --check, also exit 1 on warnings")
    parser.add_argument("--out", default=None,
                        help="write the report to a file")
    return parser


def main(argv=None):
    """Run the CLI and return the exit code."""
    args = build_parser().parse_args(argv)
    if args.max_variations < 0:
        print("error: --max-variations must be at least 0",
              file=sys.stderr)
        return 2
    as_of = parse_as_of(args.as_of)
    if as_of is None:
        print("error: --as-of %r is not a readable date"
              % args.as_of, file=sys.stderr)
        return 2
    try:
        variations, findings = parse_csv(args.csv_file, as_of)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    findings.extend(audit(variations, as_of,
                          max_variations=args.max_variations))
    summary = summarize(variations, findings)
    if args.json:
        report = render_json(variations, findings, summary)
    else:
        report = render_text(variations, findings, summary)
    if args.out is not None:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(report)
        except OSError as exc:
            print("error: cannot write %s: %s" % (args.out, exc),
                  file=sys.stderr)
            return 2
    else:
        sys.stdout.write(report)
    if args.check:
        if summary["errors"] > 0:
            return 1
        if args.strict and summary["warnings"] > 0:
            return 1
    return 0


def main_entry():
    """Console script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    main_entry()
