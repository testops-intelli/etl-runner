"""Comparing what production holds against what the mapping says it should.

The runner reports completeness: every source row was inserted. That is not
correctness. This module answers the second question by re-deriving every
target value from the staging row and the registry's own rules, then diffing
that against what is actually in production.

Re-deriving rather than checksumming is the point. Staging is TEXT and
production is typed, so a raw hash across the two can never match; and a hash
of what the runner wrote only ever proves the runner agrees with itself. The
comparison here is an independent evaluation of the mapping.

What it therefore CANNOT catch: a mapping that is wrong. If a LOOKUP resolves
against the wrong reference table, this module re-derives using that same wrong
rule and reports agreement. Cross-table aggregate checks are what catch that
class of error, which is why they are declared separately rather than derived.
"""

import datetime as dt
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .naming import quote_ident
from .transform import RowError, transform_row

# Numeric target types whose totals are worth summing automatically.
NUMERIC_TYPES = {
    "smallint", "integer", "bigint",
    "numeric", "decimal", "real", "double precision",
}


# --------------------------------------------------------------------------
# Value comparison
# --------------------------------------------------------------------------

def comparable(value):
    """Canonical form of a value for equality and hashing.

    NUMERIC(20,4) hands back Decimal('506525.0000') where the cast of the
    staging text produced Decimal('506525'). Those are the same number and
    must compare equal, so numbers are reduced to a canonical decimal string
    with no trailing zeros and no exponent notation.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, Decimal):
        return ("num", _decimal_key(value))
    if isinstance(value, int):
        return ("num", _decimal_key(Decimal(value)))
    if isinstance(value, float):
        return ("num", _decimal_key(Decimal(str(value))))
    if isinstance(value, dt.datetime):
        return ("ts", value.isoformat())
    if isinstance(value, dt.date):
        return ("date", value.isoformat())
    return ("text", str(value))


def _decimal_key(number: Decimal) -> str:
    if number.is_nan() or number.is_infinite():
        return str(number)
    # normalize() strips trailing zeros but can yield 1E+3; 'f' formatting
    # forces plain notation so 1000 and 1000.0000 share one representation.
    return format(number.normalize(), "f")


def values_equal(left, right) -> bool:
    return comparable(left) == comparable(right)


def render(value) -> str:
    """Human-readable form of a value for a report cell."""
    if value is None:
        return "(null)"
    if isinstance(value, Decimal):
        return _decimal_key(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def build_key(row: Dict, key_columns: List[str]) -> Tuple:
    return tuple(comparable(row.get(column)) for column in key_columns)


def render_key(row: Dict, key_columns: List[str]) -> str:
    return " | ".join(
        "{}={}".format(column, render(row.get(column)))
        for column in key_columns
    )


# --------------------------------------------------------------------------
# Deriving and fetching
# --------------------------------------------------------------------------

def derive_expected(rules: List[Dict], target_types: Dict[str, str],
                    source_rows: List[Dict],
                    lookup_caches: Dict) -> Tuple[List[Dict], List[Dict]]:
    """Re-derive the target row each staging row should have produced.

    Returns (expected_rows, derivation_errors). A derivation error means the
    mapping cannot produce a value at all - an unmapped code, an unresolvable
    lookup. The runner would have failed that row too, so production should
    not contain it.
    """
    expected: List[Dict] = []
    errors: List[Dict] = []
    for position, source_row in enumerate(source_rows, start=1):
        try:
            values = transform_row(
                rules, target_types, source_row, lookup_caches)
        except RowError as exc:
            errors.append({
                "row": position,
                "column": exc.target_column,
                "reason": exc.reason,
            })
            continue
        values["__source_row__"] = position
        expected.append(values)
    return expected, errors


def fetch_target_rows(cursor, schema: str, table: str,
                      columns: List[str]) -> List[Dict]:
    column_list = ", ".join(quote_ident(name) for name in columns)
    cursor.execute("SELECT {} FROM {}.{};".format(
        column_list, quote_ident(schema), quote_ident(table)))
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


# --------------------------------------------------------------------------
# Row-level comparison
# --------------------------------------------------------------------------

def compare_rows(expected: List[Dict], actual: List[Dict],
                 key_columns: List[str],
                 compare_columns: List[str]) -> Dict:
    """Diff derived rows against production rows on the identifying key.

    Reports four distinct conditions rather than one count, because they mean
    different things: a missing row is data that never arrived, an orphan is
    data production holds that the source does not explain, a duplicate key is
    the append-only insert path having run twice, and a value mismatch is a
    row that arrived carrying the wrong contents.
    """
    actual_by_key: Dict[Tuple, List[Dict]] = {}
    for row in actual:
        actual_by_key.setdefault(build_key(row, key_columns), []).append(row)

    expected_by_key: Dict[Tuple, List[Dict]] = {}
    for row in expected:
        expected_by_key.setdefault(build_key(row, key_columns), []).append(row)

    missing: List[Dict] = []
    matched = 0
    mismatches: List[Dict] = []
    duplicates: List[Dict] = []

    for key, rows in expected_by_key.items():
        candidates = actual_by_key.get(key)
        if not candidates:
            for row in rows:
                missing.append({
                    "source_row": row.get("__source_row__"),
                    "key": render_key(row, key_columns),
                })
            continue

        if len(candidates) > len(rows):
            duplicates.append({
                "key": render_key(rows[0], key_columns),
                "expected_count": len(rows),
                "actual_count": len(candidates),
            })

        # Compare positionally; identical keys make any pairing equivalent.
        for offset, expected_row in enumerate(rows):
            if offset >= len(candidates):
                missing.append({
                    "source_row": expected_row.get("__source_row__"),
                    "key": render_key(expected_row, key_columns),
                })
                continue
            actual_row = candidates[offset]
            row_ok = True
            for column in compare_columns:
                if column in key_columns:
                    continue
                if not values_equal(expected_row.get(column),
                                    actual_row.get(column)):
                    row_ok = False
                    mismatches.append({
                        "source_row": expected_row.get("__source_row__"),
                        "key": render_key(expected_row, key_columns),
                        "column": column,
                        "expected": render(expected_row.get(column)),
                        "actual": render(actual_row.get(column)),
                    })
            if row_ok:
                matched += 1

    orphans: List[Dict] = []
    for key, rows in actual_by_key.items():
        surplus = len(rows) - len(expected_by_key.get(key, []))
        for row in rows[len(rows) - surplus:] if surplus > 0 else []:
            orphans.append({"key": render_key(row, key_columns)})

    return {
        "matched": matched,
        "missing": missing,
        "orphans": orphans,
        "duplicates": duplicates,
        "mismatches": mismatches,
    }


# --------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------

def derived_aggregates(rules: List[Dict], target_columns: List[Dict],
                       expected: List[Dict], cursor,
                       target_schema: str, target_table: str) -> List[Dict]:
    """Aggregate checks the registry already knows enough to build.

    Row counts on both sides, and a total for every numeric column carried
    across by a DIRECT rule. A sum over LOOKUP-resolved surrogate keys would
    be arithmetic on identifiers and is deliberately not produced.
    """
    results: List[Dict] = []

    cursor.execute("SELECT COUNT(*) FROM {}.{};".format(
        quote_ident(target_schema), quote_ident(target_table)))
    actual_count = cursor.fetchone()[0]
    results.append(_aggregate_result(
        "row count",
        "rows derived from staging", Decimal(len(expected)),
        "rows in {}.{}".format(target_schema, target_table),
        Decimal(actual_count), Decimal(0)))

    types = {column["name"]: column["data_type"] for column in target_columns}
    direct_columns = [
        rule["target_column"] for rule in rules
        if rule["rule_type"] == "DIRECT"
        and (types.get(rule["target_column"]) or "").lower() in NUMERIC_TYPES
    ]

    for column in direct_columns:
        expected_total = Decimal(0)
        for row in expected:
            value = row.get(column)
            if value is not None:
                expected_total += Decimal(str(value))
        cursor.execute("SELECT COALESCE(SUM({}), 0) FROM {}.{};".format(
            quote_ident(column), quote_ident(target_schema),
            quote_ident(target_table)))
        actual_total = cursor.fetchone()[0]
        results.append(_aggregate_result(
            "sum of {}".format(column),
            "derived from staging", expected_total,
            "{}.{}.{}".format(target_schema, target_table, column),
            Decimal(str(actual_total or 0)), Decimal(0)))

    return results


def declared_aggregates(cursor, checks: List[Dict]) -> List[Dict]:
    """Run the cross-table invariants recorded by the wizard."""
    results = []
    for check in checks:
        left = _evaluate_side(
            cursor, check["left_function"], check["left_schema"],
            check["left_table"], check["left_column"])
        right = _evaluate_side(
            cursor, check["right_function"], check["right_schema"],
            check["right_table"], check["right_column"])
        results.append(_aggregate_result(
            check["check_label"],
            _side_label(check, "left"), left,
            _side_label(check, "right"), right,
            Decimal(str(check["tolerance"] or 0))))
    return results


def _side_label(check: Dict, side: str) -> str:
    from .registry import describe_aggregate_side
    return describe_aggregate_side(
        check["{}_function".format(side)], check["{}_schema".format(side)],
        check["{}_table".format(side)], check["{}_column".format(side)])


def _evaluate_side(cursor, function: str, schema: str, table: str,
                   column: Optional[str]) -> Optional[Decimal]:
    reference = "{}.{}".format(quote_ident(schema), quote_ident(table))
    if function == "COUNT":
        cursor.execute("SELECT COUNT(*) FROM {};".format(reference))
    elif function == "VALUE":
        # A single scalar held in a one-row table, such as the company's
        # shares outstanding. More than one row makes the check meaningless.
        cursor.execute("SELECT {} FROM {};".format(
            quote_ident(column), reference))
        rows = cursor.fetchall()
        if len(rows) != 1:
            return None
        return None if rows[0][0] is None else Decimal(str(rows[0][0]))
    else:
        cursor.execute("SELECT {}({}) FROM {};".format(
            function, quote_ident(column), reference))
    value = cursor.fetchone()[0]
    return None if value is None else Decimal(str(value))


def _aggregate_result(label, left_label, left_value,
                      right_label, right_value, tolerance) -> Dict:
    if left_value is None or right_value is None:
        status, variance = "FAIL", None
    else:
        variance = left_value - right_value
        status = "PASS" if abs(variance) <= tolerance else "FAIL"
    return {
        "label": label,
        "left_label": left_label,
        "left_value": left_value,
        "right_label": right_label,
        "right_value": right_value,
        "variance": variance,
        "tolerance": tolerance,
        "status": status,
    }


# --------------------------------------------------------------------------
# Column coverage
# --------------------------------------------------------------------------

def column_coverage(target_columns: List[Dict], rules: List[Dict],
                    key_columns: List[str]) -> List[Dict]:
    """What happened to every target column, including the ones not checked.

    A column nobody validated is stated rather than left out of the report,
    for the same reason a discarded source column is recorded rather than
    silently dropped: an omission should be visible as a decision.
    """
    from .registry import describe_rule

    by_column = {rule["target_column"]: rule for rule in rules}
    coverage = []
    for column in target_columns:
        name = column["name"]
        rule = by_column.get(name)
        if column["is_auto_generated"]:
            treatment = "database-generated"
            checked = "no validation required"
        elif rule is None:
            treatment = "no rule in the mapping"
            checked = "not validated"
        else:
            treatment = describe_rule(rule)
            checked = "identifying key" if name in key_columns else "compared"
        coverage.append({
            "column": name,
            "data_type": column["data_type"],
            "treatment": treatment,
            "validation": checked,
        })
    return coverage
