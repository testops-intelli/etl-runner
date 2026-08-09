"""Applying mapping rules to staging rows, and casting to target types.

Staging columns are all TEXT. Casting therefore happens here, at transform
time, where a failure can be attributed to a specific row and column rather
than aborting a bulk load with no indication of which record was bad.
"""

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from .naming import quote_ident


class RowError(Exception):
    """A single row failed to transform. Carries the offending column."""

    def __init__(self, target_column: str, reason: str):
        super().__init__(reason)
        self.target_column = target_column
        self.reason = reason


TRUE_TOKENS = {"true", "t", "yes", "y", "1"}
FALSE_TOKENS = {"false", "f", "no", "n", "0"}

DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
    "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
)
TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%d",
)

_EXPRESSION_TOKEN = re.compile(r"\{([a-zA-Z0-9_]+)\}")


# --------------------------------------------------------------------------
# Target type introspection
# --------------------------------------------------------------------------

def get_target_columns(cursor, schema: str, table: str) -> List[Dict]:
    """Return target columns with type and identity/default information."""
    cursor.execute(
        """
        SELECT column_name, data_type, is_nullable,
               column_default, is_identity
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position;
        """,
        (schema, table),
    )
    columns = []
    for name, data_type, is_nullable, default, is_identity in cursor.fetchall():
        auto = (is_identity == "YES") or (
            default is not None and str(default).startswith("nextval(")
        )
        columns.append({
            "name": name,
            "data_type": data_type,
            "nullable": is_nullable == "YES",
            "is_auto_generated": auto,
        })
    return columns


def get_stage_columns(cursor, schema: str, table: str) -> List[str]:
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position;
        """,
        (schema, table),
    )
    return [row[0] for row in cursor.fetchall()]


def table_exists(cursor, schema: str, table: str) -> bool:
    cursor.execute("SELECT to_regclass(%s);", ("{}.{}".format(schema, table),))
    return cursor.fetchone()[0] is not None


# --------------------------------------------------------------------------
# Casting
# --------------------------------------------------------------------------

def cast_value(raw, data_type: str, target_column: str):
    """Cast a text value to the Python type matching the target column."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "":
            return None

    kind = (data_type or "").lower()

    try:
        if kind in ("smallint", "integer", "bigint"):
            # Accept "1535000" and "1535000.0"; reject "1535000.5".
            number = Decimal(str(raw))
            if number != number.to_integral_value():
                raise ValueError(
                    "value '{}' is not a whole number".format(raw)
                )
            return int(number)

        if kind in ("numeric", "decimal", "real", "double precision"):
            return Decimal(str(raw))

        if kind == "boolean":
            token = str(raw).strip().lower()
            if token in TRUE_TOKENS:
                return True
            if token in FALSE_TOKENS:
                return False
            raise ValueError("value '{}' is not a recognised boolean".format(raw))

        if kind == "date":
            if isinstance(raw, dt.datetime):
                return raw.date()
            if isinstance(raw, dt.date):
                return raw
            return _parse_temporal(str(raw), DATE_FORMATS, as_date=True)

        if kind.startswith("timestamp"):
            if isinstance(raw, dt.datetime):
                return raw
            return _parse_temporal(str(raw), TIMESTAMP_FORMATS, as_date=False)

        # text, varchar, char, uuid and anything else pass through as text.
        return str(raw)

    except (ValueError, InvalidOperation, ArithmeticError) as exc:
        raise RowError(
            target_column,
            "cannot cast '{}' to {} ({})".format(raw, data_type, exc),
        )


def _parse_temporal(text: str, formats, as_date: bool):
    for fmt in formats:
        try:
            parsed = dt.datetime.strptime(text, fmt)
            return parsed.date() if as_date else parsed
        except ValueError:
            continue
    raise ValueError(
        "value '{}' does not match any accepted date format".format(text)
    )


# --------------------------------------------------------------------------
# Lookup caching
# --------------------------------------------------------------------------

def build_lookup_caches(cursor, rules: List[Dict]) -> Dict[int, Dict[str, object]]:
    """Preload each LOOKUP rule's match->return pairs into memory.

    One query per lookup rule rather than one per row. Matching is done on the
    text form of the key, case-insensitively, which is what makes name-based
    resolution survive incidental case differences between systems.
    """
    caches: Dict[int, Dict[str, object]] = {}
    for rule in rules:
        if rule["rule_type"] != "LOOKUP":
            continue
        sql = "SELECT {match}, {ret} FROM {schema}.{table};".format(
            match=quote_ident(rule["lookup_match_column"]),
            ret=quote_ident(rule["lookup_return_column"]),
            schema=quote_ident(rule["lookup_schema"]),
            table=quote_ident(rule["lookup_table"]),
        )
        cursor.execute(sql)
        cache = {}
        for match_value, return_value in cursor.fetchall():
            if match_value is None:
                continue
            cache[str(match_value).strip().lower()] = return_value
        caches[rule["rule_id"]] = cache
    return caches


# --------------------------------------------------------------------------
# Rule evaluation
# --------------------------------------------------------------------------

def resolve_rule(rule: Dict, source_row: Dict[str, str],
                 lookup_caches: Dict[int, Dict[str, object]]):
    """Produce the pre-cast value for one target column of one row."""
    rule_type = rule["rule_type"]
    target_column = rule["target_column"]

    if rule_type == "NULL":
        return None

    if rule_type == "CONSTANT":
        return rule["constant_value"]

    if rule_type == "DIRECT":
        return source_row.get(rule["source_column"])

    if rule_type == "VALUE_MAP":
        raw = source_row.get(rule["source_column"])
        if raw is None or str(raw).strip() == "":
            return None
        key = str(raw).strip().upper()
        mapping = {k.strip().upper(): v for k, v in rule["value_map"].items()}
        if key in mapping:
            return mapping[key]
        if rule["allow_unmapped"]:
            return rule["unmapped_default"]
        raise RowError(
            target_column,
            "value '{}' has no entry in the value map and no fallback is "
            "configured for this rule".format(raw),
        )

    if rule_type == "LOOKUP":
        raw = source_row.get(rule["source_column"])
        if raw is None or str(raw).strip() == "":
            return None
        cache = lookup_caches.get(rule["rule_id"], {})
        key = str(raw).strip().lower()
        if key in cache:
            return cache[key]
        if rule["allow_unmapped"]:
            return rule["unmapped_default"]
        raise RowError(
            target_column,
            "value '{}' not found in {}.{}.{}".format(
                raw, rule["lookup_schema"], rule["lookup_table"],
                rule["lookup_match_column"],
            ),
        )

    if rule_type == "EXPRESSION":
        template = rule["expression_template"] or ""

        def substitute(match):
            column = match.group(1)
            if column not in source_row:
                raise RowError(
                    target_column,
                    "expression references unknown source column "
                    "'{}'".format(column),
                )
            value = source_row.get(column)
            return "" if value is None else str(value)

        return _EXPRESSION_TOKEN.sub(substitute, template)

    raise RowError(target_column, "unknown rule type '{}'".format(rule_type))


def transform_row(rules: List[Dict], target_types: Dict[str, str],
                  source_row: Dict[str, str],
                  lookup_caches: Dict[int, Dict[str, object]]) -> Dict[str, object]:
    """Apply every rule to one staging row, returning target column values."""
    output: Dict[str, object] = {}
    for rule in rules:
        target_column = rule["target_column"]
        raw = resolve_rule(rule, source_row, lookup_caches)
        output[target_column] = cast_value(
            raw, target_types.get(target_column, "text"), target_column
        )
    return output


def validate_rules(rules: List[Dict], stage_columns: List[str],
                   target_columns: List[str]) -> List[str]:
    """Check rules against the live schema before any row is processed."""
    problems: List[str] = []
    stage_set = set(stage_columns)
    target_set = set(target_columns)

    for rule in rules:
        target_column = rule["target_column"]
        if target_column not in target_set:
            problems.append(
                "target column '{}' in the mapping no longer exists in the "
                "target table".format(target_column)
            )

        needs_source = rule["rule_type"] in ("DIRECT", "VALUE_MAP", "LOOKUP")
        if needs_source:
            source_column = rule["source_column"]
            if not source_column:
                problems.append(
                    "rule for '{}' is {} but has no source column".format(
                        target_column, rule["rule_type"]
                    )
                )
            elif source_column not in stage_set:
                problems.append(
                    "rule for '{}' reads source column '{}', which is not in "
                    "the staging table".format(target_column, source_column)
                )

        if rule["rule_type"] == "EXPRESSION":
            for referenced in _EXPRESSION_TOKEN.findall(
                rule["expression_template"] or ""
            ):
                if referenced not in stage_set:
                    problems.append(
                        "expression for '{}' references '{}', which is not in "
                        "the staging table".format(target_column, referenced)
                    )

    return problems
