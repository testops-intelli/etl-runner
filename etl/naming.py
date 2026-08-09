"""Turning arbitrary source-file headers into safe SQL identifiers.

Real client extracts do not arrive with SQL-safe column names. The files used
to develop this framework contained, among others:

    "Communication Preference"   -> space in the header
    "$ per unit"                 -> leading symbol
    "stock split ratio"          -> spaces
    "Holder_ID" (twice)          -> duplicate header in the same file

Every one of those breaks a naive CREATE TABLE. The rules below are applied at
ingestion time, and every change is reported to the user rather than made
silently. No column is ever dropped to resolve a naming collision.
"""

import re
from typing import List, Tuple

MAX_IDENTIFIER_LENGTH = 63  # PostgreSQL truncates beyond this.

# Words that cannot be used bare as a column name.
RESERVED_WORDS = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "both",
    "case", "cast", "check", "collate", "column", "constraint", "create",
    "current_date", "current_time", "current_timestamp", "current_user",
    "default", "deferrable", "desc", "distinct", "do", "else", "end", "except",
    "false", "for", "foreign", "from", "grant", "group", "having", "in",
    "initially", "intersect", "into", "lateral", "leading", "limit",
    "localtime", "localtimestamp", "not", "null", "offset", "on", "only", "or",
    "order", "placing", "primary", "references", "returning", "select",
    "session_user", "some", "symmetric", "table", "then", "to", "trailing",
    "true", "union", "unique", "user", "using", "variadic", "when", "where",
    "window", "with",
}

# Symbols that carry meaning worth preserving in a column name.
SYMBOL_WORDS = {
    "$": "amount",
    "%": "pct",
    "#": "num",
    "&": "and",
    "@": "at",
}


def normalize_identifier(raw: str) -> str:
    """Convert a single raw header into a safe snake_case SQL identifier."""
    text = (raw or "").strip()
    if not text:
        return "unnamed_column"

    for symbol, word in SYMBOL_WORDS.items():
        text = text.replace(symbol, " {} ".format(word))

    # Split camelCase / PascalCase before lowercasing so ExDate -> ex_date.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)

    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")

    if not text:
        return "unnamed_column"

    # An identifier cannot begin with a digit.
    if text[0].isdigit():
        text = "col_" + text

    if text in RESERVED_WORDS:
        text = text + "_col"

    return text[:MAX_IDENTIFIER_LENGTH]


def normalize_headers(raw_headers: List[str]) -> Tuple[List[str], List[str]]:
    """Normalize a full header row, resolving duplicates by numeric suffix.

    Returns (normalized_headers, notes) where notes describes every change made
    so the caller can print it. Duplicate headers are suffixed _2, _3, ... in
    the order encountered; the first occurrence keeps the unsuffixed name.
    """
    normalized: List[str] = []
    notes: List[str] = []
    seen: dict = {}

    for position, raw in enumerate(raw_headers, start=1):
        base = normalize_identifier(raw)
        raw_display = (raw or "").strip() or "(blank)"

        if base in seen:
            seen[base] += 1
            candidate = "{}_{}".format(base, seen[base])
            # Guard against a suffixed name colliding with a real column.
            while candidate in normalized:
                seen[base] += 1
                candidate = "{}_{}".format(base, seen[base])
            notes.append(
                "column {}: duplicate header '{}' renamed to '{}' "
                "(no column dropped)".format(position, raw_display, candidate)
            )
            normalized.append(candidate)
        else:
            seen[base] = 1
            # Pure case changes are not worth reporting; anything else is.
            if raw_display.lower() != base:
                notes.append(
                    "column {}: '{}' normalized to '{}'".format(
                        position, raw_display, base
                    )
                )
            normalized.append(base)

    return normalized, notes


def quote_ident(name: str) -> str:
    """Double-quote an identifier for safe interpolation into DDL/DML."""
    return '"{}"'.format(str(name).replace('"', '""'))


def qualify(schema: str, table: str) -> str:
    return "{}.{}".format(quote_ident(schema), quote_ident(table))
