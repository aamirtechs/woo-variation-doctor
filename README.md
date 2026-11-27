# woo-variation-doctor

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/aamirtechs/woo-variation-doctor/blob/main/LICENSE) [![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/aamirtechs/woo-variation-doctor/issues)

A zero-dependency Python CLI that audits a WooCommerce
product variations CSV export for the variation problems
that silently remove options from the storefront:
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

## Install

Requires Python 3.9 or newer. No dependencies beyond the
standard library.

```bash
pip install woo-variation-doctor
```

Or run it straight from the source checkout:

```bash
python3 woo_variation_doctor.py examples/variations.csv --as-of 2026-11-18
```

## Input format

A CSV export with a header row, one row per variation.
Columns used: `parent_id` (or `parent`,
`parent_product_id`, `product_id`), `variation_id` (or
`variation`, `id`), `attributes`, `regular_price`,
`sale_price`, `sale_start`, `sale_end`, `stock`, `sku`,
`enabled`, and `backorder`. Extra columns are ignored, so
a full variations export works as-is. A UTF-8 BOM header
is handled. Only `variation_id` is required; the other
columns are optional, and the checks that need a missing
column are skipped.

`regular_price` also accepts `regular` or `price`;
`sale_price` also accepts `sale`; `sale_start` also
accepts `sale_start_date` or `sale_from`; `sale_end` also
accepts `sale_end_date`, `sale_to`, or `sale_until`;
`stock` also accepts `stock_quantity`, `quantity`, or
`stock_qty`; `enabled` also accepts `active` or `status`;
`backorder` also accepts `backorders`,
`allow_backorders`, or `backordered`; `attributes` also
accepts `attribute_values`, `options`, or `attribute`.

The `attributes` cell holds semicolon separated
name:value pairs, for example `Size:M;Color:Red`. The
`enabled` and `backorder` cells take `yes`, `true`, `1`,
or `y` for yes, and `no`, `false`, `0`, or `n` for no; an
empty `enabled` cell means enabled, and an empty
`backorder` cell means no backorder. Dates accept
`2026-11-18`, `2026/11/18`, and ISO datetimes. Prices
accept a plain number with an optional dollar sign and
thousands commas.

```csv
parent_id,variation_id,attributes,regular_price,sale_price,sale_start,sale_end,stock,sku,enabled,backorder
P-100,V-101,Size:S;Color:Red,29.99,24.99,2026-11-01,2026-11-30,12,TS-S-RED,yes,no
P-100,V-102,Size:M;Color:Red,29.99,,,,8,TS-M-RED,yes,no
P-100,V-103,Size:L;Color:Red,29.99,34.99,2026-11-01,2026-11-30,5,TS-L-RED,yes,no
P-100,V-104,Size:S;Color:Blue,,,,,3,TS-S-BLU,yes,no
```

## What it checks

Errors, the ones that mean a variation cannot sell
correctly or is recorded wrong:

* `no-price`: a variation with no regular price, so it is
  not purchasable even when the parent is published. A
  variation whose price is invalid gets `invalid-price`
  instead, not this one.
* `sale-above-regular`: a sale price above the regular
  price.
* `expired-sale-active`: a sale price still set on an
  enabled variation whose sale end date has passed.
* `invalid-sale-dates`: a sale start date after the sale
  end date. When this fires, the expired sale check is
  skipped for that variation.
* `duplicate-attributes`: an attribute combination already
  used by another variation on the same parent, so the
  two variations are the same option. Comparing ignores
  order and case, so `Color:Red;Size:S` matches
  `Size:S;Color:Red`.
* `duplicate-sku`: a SKU already used by another
  variation, so orders may pick the wrong variation.
  Matching ignores case. An empty SKU is allowed.
* `duplicate-variation`: a variation id that appears more
  than once.
* `malformed-row`: a row with the wrong number of
  fields. The row is skipped and reported, never a
  crash.
* `empty-variation`: a row with no variation id. The row
  is skipped and reported.
* `invalid-price`: a regular or sale price that is not a
  valid non-negative price.
* `invalid-stock`: stock that is not a valid non-negative
  whole number.
* `invalid-date`: a sale start or sale end date that
  matches no known form (use `2026-11-18`).
* `invalid-attribute`: an attribute entry that is not a
  name:value pair (use `Size:M`).
* `invalid-flag`: an enabled or backorder value that is
  not yes or no.

Warnings, patterns worth investigating:

* `sale-no-end`: a sale price with no end date, so the
  sale runs forever unless someone removes it.
* `stock-but-disabled`: a variation with stock that is
  disabled, so the stock cannot sell.
* `zero-stock-enabled`: a variation enabled with zero
  stock and no backorder, so customers see an option they
  cannot buy.
* `too-many-variations`: a parent with more variations
  than `--max-variations` (default 50), so the product
  may be too complex to shop.

The summary reports variations, parents, findings,
errors, warnings, variations on sale, variations with no
price, and duplicate SKUs, with a per-parent table of
variation count, minimum and maximum regular price, and
variations on sale.

## Usage

```bash
python3 woo_variation_doctor.py examples/variations.csv --as-of 2026-11-18
```

Captured output:

```
ERROR [duplicate-sku] V-203: SKU TS-S-RED is already used by another variation, so orders may pick the wrong variation
ERROR [invalid-price] V-205: regular_price 'abc' is not a valid non-negative price
ERROR [invalid-price] V-206: regular_price '-10.00' is not a valid non-negative price
ERROR [malformed-row] row 22: row has 3 fields but the header has 11, so the row was skipped
ERROR [empty-variation] row 23: row has no variation id, so the row was skipped
ERROR [invalid-date] V-404: sale_start '2026-13-01' is not a readable date (use 2026-11-18)
ERROR [sale-above-regular] V-103: sale price $34.99 is above the regular price $29.99
ERROR [no-price] V-104: variation has no regular price, so it is not purchasable even when the parent is published
WARNING [sale-no-end] V-105: sale price $19.99 has no end date, so the sale runs forever unless someone removes it
WARNING [zero-stock-enabled] V-105: variation is enabled with zero stock and no backorder, so customers see an option they cannot buy
ERROR [expired-sale-active] V-106: sale ended 2026-10-31 but the sale price $14.99 is still set and the variation is still enabled
ERROR [duplicate-attributes] V-107: attribute combination Size:S;Color:Red is already used by variation V-101 on parent P-100, so the two variations are the same option
WARNING [stock-but-disabled] V-204: variation has 15 in stock but is disabled, so the stock cannot sell
ERROR [invalid-sale-dates] V-302: sale start 2026-11-15 is after sale end 2026-11-10
WARNING [zero-stock-enabled] V-303: variation is enabled with zero stock and no backorder, so customers see an option they cannot buy
WARNING [zero-stock-enabled] V-405: variation is enabled with zero stock and no backorder, so customers see an option they cannot buy

Variations: 23, parents: 4, findings: 16, errors: 11, warnings: 5
On sale: 7, variations with no price: 1, duplicate SKUs: 1

Per-parent summary:
parent     variations   min price   max price on sale
P-100               8      $29.99      $34.99       4
P-200               6      $49.99      $49.99       1
P-300               4      $14.99      $14.99       1
P-400               5      $19.99      $19.99       1
```

The sample has 25 rows; the malformed row 22 and the
row with no variation id are skipped, so 23 variations
are audited across 4 parents. The headline cases are
V-104 with no price at all, V-103 on sale at $34.99
above its $29.99 regular price, V-106 whose sale ended
2026-10-31 but is still active, V-107 duplicating
V-101's Size S Red combination, V-203 reusing V-101's
TS-S-RED SKU, V-204 holding 15 in stock while disabled,
and V-202, which is enabled with zero stock but has
backorders allowed, so it correctly gets no warning.

Use it as a CI gate: `--check` exits 1 when any error is
found, and 0 otherwise. On the sample above it exits 1:

```bash
python3 woo_variation_doctor.py examples/variations.csv --as-of 2026-11-18 --check
```

On `examples/clean.csv`, three clean variations, it
exits 0:

```
Variations: 3, parents: 2, findings: 0, errors: 0, warnings: 0
On sale: 1, variations with no price: 0, duplicate SKUs: 0

Per-parent summary:
parent     variations   min price   max price on sale
P-100               2      $29.99      $29.99       1
P-200               1      $49.99      $49.99       0

All variations clean.
```

With `--strict`, `--check` also fails on warnings.
`examples/warnings-only.csv` has one sale with no end
date and two zero stock enabled variations, so it passes
`--check` (exit 0) and fails `--check --strict`
(exit 1). With an earlier as-of date the expired sale
drops out of the sample report:

```bash
python3 woo_variation_doctor.py examples/variations.csv --as-of 2026-10-15 --check
```

That run still exits 1 on the sample's errors, but the
`expired-sale-active` error for V-106 is gone, leaving
15 findings, 10 errors, and 5 warnings. With a low
variation limit the largest parents are flagged:

```bash
python3 woo_variation_doctor.py examples/variations.csv --as-of 2026-11-18 --max-variations 5 --check
```

That run adds `too-many-variations` warnings for P-100
(8 variations) and P-200 (6 variations), leaving 18
findings, 11 errors, and 7 warnings.

`--json` emits the summary, the per-parent table, and
the findings as JSON (money as strings, to keep the
decimals exact), and `--out` writes the report to a
file.

## Options

| Flag | Meaning |
| --- | --- |
| `--max-variations` | A parent with more variations than this is flagged (default: 50) |
| `--min-price DOLLARS` | Warn for every variation whose regular price is below this floor (no floor is checked when the flag is absent) |
| `--as-of` | Date to compare sale dates against, for example 2026-11-18 (default: now) |
| `--json` | Emit JSON instead of text |
| `--check` | Exit 1 if any error is found |
| `--strict` | With --check, also exit 1 on warnings |
| `--out` | Write the report to a file |

## Edge cases

* A row with too many or too few fields is a malformed-row
  error and is skipped; the rest of the file is still
  audited.
* A row with no variation id is an empty-variation error
  and is skipped.
* A bad price, stock value, or date is an error, and the
  row is still audited on the fields that did parse, so
  one bad value never hides the other problems on the
  row.
* A variation with an invalid regular price gets
  invalid-price, not no-price, so the two problems stay
  distinct.
* A sale that ends today is not expired; a sale that
  ended yesterday is. An expired sale on a disabled
  variation is not flagged, since the variation is not
  selling.
* A sale start after the sale end is an invalid-sale-dates
  error, and the expired sale check is skipped for that
  variation.
* Two variations with empty attributes on the same parent
  count as duplicates, since both are the same "any"
  option.
* A variation with no parent id is grouped under
  "(no parent)" in the per-parent table, and duplicate
  attribute checks are skipped for it.
* Money uses Decimal math, so price ranges add up
  exactly.
* A blank line in the file is skipped silently.
* `--as-of` makes runs reproducible: without it, the
  sale date checks compare against the current time.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

84 tests, all passing: money and stock parsing, flag and
date parsing, attributes cell parsing, header aliases and
BOM handling, extra columns, malformed rows, empty
variation ids, invalid prices, stock, dates, attributes,
and flags, duplicate variations, SKUs, and attribute
combinations, no-price, sale above regular, sale with no
end, expired sales and boundaries, sale start after end,
stock but disabled, zero stock enabled, backorder
exemption, too many variations and boundaries, summary
counts, per-parent price ranges, text and JSON output,
and `--check` and `--strict` exit codes. The same 84
tests pass under pytest.

## License

MIT. See [LICENSE](LICENSE).
