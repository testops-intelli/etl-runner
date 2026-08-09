"""Validation wizard: record what identifies a row, and what must reconcile.

    python scripts/validate_wizard.py --mapping holdings_to_prod

Two things are captured, and neither can be inferred from the mapping.

The identifying key. A target table's primary key is a surrogate issued at
insert time, so it cannot be matched back to a staging row. Business identity
has to be stated. Only target-side columns are asked for: the validator
re-derives every target column from the mapping, so the staging side follows
from whichever source columns feed the ones chosen here.

Cross-table invariants. That holdings units should total the company's shares
outstanding is a fact about share registries, not about column types or rules.
Nothing in the registry implies it, so it is declared rather than derived -
and it is the only class of check that catches a mapping which is wrong rather
than incomplete.

Nothing is written until the summary is confirmed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from etl.config import load_config
from etl.registry import (
    AGGREGATE_FUNCTIONS,
    describe_aggregate_side,
    get_aggregate_checks,
    get_column_rules,
    get_mapping_set,
    get_validation_key,
    list_mapping_sets,
    replace_aggregate_checks,
    replace_validation_key,
)
from etl.transform import get_target_columns, table_exists
from etl.validation import NUMERIC_TYPES


def prompt(message: str, default: str = None) -> str:
    suffix = " [{}]".format(default) if default else ""
    while True:
        answer = input("{}{}: ".format(message, suffix)).strip()
        if answer:
            return answer
        if default is not None:
            return default


def prompt_optional(message: str) -> str:
    return input("{}: ".format(message)).strip()


def prompt_yes_no(message: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input("{} ({}): ".format(message, hint)).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def choose_from(items, message: str):
    for position, item in enumerate(items, start=1):
        print("  {}. {}".format(position, item))
    while True:
        answer = prompt(message)
        if answer in items:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print("  Not one of the options. Answer with the number or the name.")


def list_schemas(cursor):
    cursor.execute(
        """
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
          AND schema_name NOT LIKE 'pg_%'
        ORDER BY schema_name;
        """
    )
    return [row[0] for row in cursor.fetchall()]


def list_tables(cursor, schema: str):
    cursor.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name;
        """,
        (schema,),
    )
    return [row[0] for row in cursor.fetchall()]


def list_columns(cursor, schema: str, table: str):
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position;
        """,
        (schema, table),
    )
    return [row[0] for row in cursor.fetchall()]


# --------------------------------------------------------------------------
# Identifying key
# --------------------------------------------------------------------------

def choose_key_columns(cursor, mapping, target_columns, rules, current):
    """Ask which target columns identify a row, and prove the answer."""
    mappable = [c for c in target_columns if not c["is_auto_generated"]]
    rule_by_column = {rule["target_column"]: rule for rule in rules}
    auto = [c["name"] for c in target_columns if c["is_auto_generated"]]

    print()
    print("A row has to be identified by its business content, not by the")
    print("target's own primary key: that key is issued at insert time and")
    print("has no counterpart in staging.")
    if auto:
        print("Database-generated, not selectable: {}".format(", ".join(auto)))
    print()
    print("Target columns available:")
    for position, column in enumerate(mappable, start=1):
        rule = rule_by_column.get(column["name"])
        measure = ""
        if (rule and rule["rule_type"] == "DIRECT"
                and (column["data_type"] or "").lower() in NUMERIC_TYPES):
            measure = "   <- a measured value"
        print("  {}. {:<28} {}{}".format(
            position, column["name"], column["data_type"], measure))
    print()
    print("Choose the columns that together identify one row, separated by")
    print("commas. Avoid measured values: a column that is both a join key")
    print("and a compared value cannot report as wrong, only as missing.")

    names = [c["name"] for c in mappable]
    default = ", ".join(current) if current else None

    while True:
        answer = prompt("Identifying columns", default)
        chosen = []
        unknown = []
        for token in answer.split(","):
            token = token.strip()
            if not token:
                continue
            if token in names:
                chosen.append(token)
            elif token.isdigit() and 1 <= int(token) <= len(names):
                chosen.append(names[int(token) - 1])
            else:
                unknown.append(token)

        if unknown:
            print("  Not a target column: {}".format(", ".join(unknown)))
            continue
        if not chosen:
            print("  At least one column is needed.")
            continue
        if len(set(chosen)) != len(chosen):
            print("  The same column was given twice.")
            continue

        duplicates = count_duplicate_keys(
            cursor, mapping["target_schema"], mapping["target_table"], chosen)
        if duplicates:
            print()
            print("  WARNING: {} key value(s) already appear on more than one"
                  .format(duplicates))
            print("  row in {}.{}. That is either a duplicate load or a key".
                  format(mapping["target_schema"], mapping["target_table"]))
            print("  that does not identify a row.")
            if not prompt_yes_no("  Use these columns anyway?", default=False):
                continue
        else:
            print("  Verified: these columns are unique in {}.{}.".format(
                mapping["target_schema"], mapping["target_table"]))

        return chosen


def count_duplicate_keys(cursor, schema, table, columns) -> int:
    from etl.naming import quote_ident
    column_list = ", ".join(quote_ident(name) for name in columns)
    cursor.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT {cols} FROM {schema}.{table}
            GROUP BY {cols} HAVING COUNT(*) > 1
        ) AS repeated;
        """.format(cols=column_list, schema=quote_ident(schema),
                   table=quote_ident(table))
    )
    return cursor.fetchone()[0]


# --------------------------------------------------------------------------
# Cross-table invariants
# --------------------------------------------------------------------------

def build_side(cursor, label: str):
    print()
    print("{} side.".format(label))
    print("Aggregate functions:")
    print("  SUM / AVG / MIN / MAX   over one column")
    print("  COUNT                   rows in the table, no column needed")
    print("  VALUE                   a single figure held in a one-row table")
    function = choose_from(list(AGGREGATE_FUNCTIONS), "Function")

    print()
    print("Schemas:")
    schema = choose_from(list_schemas(cursor), "Schema")
    print()
    print("Tables in '{}':".format(schema))
    table = choose_from(list_tables(cursor, schema), "Table")

    column = None
    if function != "COUNT":
        print()
        print("Columns in {}.{}:".format(schema, table))
        column = choose_from(list_columns(cursor, schema, table), "Column")

    return {"function": function, "schema": schema,
            "table": table, "column": column}


def collect_aggregate_checks(cursor, existing):
    checks = []
    if existing:
        print()
        print("Invariants already recorded:")
        for check in existing:
            print("  {}".format(check["check_label"]))
        if prompt_yes_no("Keep these and add to them?", default=True):
            checks = [dict(check) for check in existing]

    print()
    print("A cross-table invariant compares two figures that must agree, for")
    print("example the units in a holdings snapshot against the company's")
    print("shares outstanding. Nothing in the mapping implies it.")

    while True:
        print()
        if not prompt_yes_no(
            "Add {}invariant?".format("another " if checks else "an "),
            default=not checks,
        ):
            break

        label = prompt("Short name for this check")
        left = build_side(cursor, "Left")
        right = build_side(cursor, "Right")

        print()
        print("A tolerance allows the two sides to differ by this much and")
        print("still pass. Leave at 0 unless rounding makes exactness wrong.")
        tolerance = prompt("Tolerance", "0")

        checks.append({
            "check_label": label,
            "left_function": left["function"], "left_schema": left["schema"],
            "left_table": left["table"], "left_column": left["column"],
            "right_function": right["function"],
            "right_schema": right["schema"], "right_table": right["table"],
            "right_column": right["column"],
            "tolerance": tolerance,
        })
        print()
        print("  Recorded: {}  =  {}".format(
            describe_aggregate_side(left["function"], left["schema"],
                                    left["table"], left["column"]),
            describe_aggregate_side(right["function"], right["schema"],
                                    right["table"], right["column"])))

    return checks


# --------------------------------------------------------------------------

def run(config, mapping_name: str) -> int:
    connection = config.connect()
    try:
        with connection:
            with connection.cursor() as cursor:
                mapping = get_mapping_set(
                    cursor, config.meta_schema, mapping_name)
                if mapping is None:
                    print("ERROR: no mapping named '{}' in the registry."
                          .format(mapping_name))
                    show_available(cursor, config)
                    return 2

                if not table_exists(cursor, mapping["target_schema"],
                                    mapping["target_table"]):
                    print("ERROR: target table {}.{} does not exist.".format(
                        mapping["target_schema"], mapping["target_table"]))
                    return 2

                rules = get_column_rules(
                    cursor, config.meta_schema, mapping["mapping_set_id"])
                if not rules:
                    print("ERROR: mapping '{}' has no column rules."
                          .format(mapping_name))
                    return 2

                target_columns = get_target_columns(
                    cursor, mapping["target_schema"], mapping["target_table"])

                print("=" * 68)
                print("VALIDATION WIZARD: {}".format(mapping_name))
                print("=" * 68)
                print("Source : {}.{}".format(
                    mapping["source_schema"], mapping["source_table"]))
                print("Target : {}.{}".format(
                    mapping["target_schema"], mapping["target_table"]))
                if mapping["row_filter"]:
                    print("Filter : {}".format(mapping["row_filter"]))

                current_key = get_validation_key(
                    cursor, config.meta_schema, mapping["mapping_set_id"])
                current_checks = get_aggregate_checks(
                    cursor, config.meta_schema, mapping["mapping_set_id"])

                if current_key:
                    print()
                    print("-" * 68)
                    print("Validation is already configured for this mapping.")
                    print("  Identifying key : {}".format(
                        " + ".join(current_key)))
                    print("  Invariants      : {}".format(
                        len(current_checks)))
                    print("-" * 68)
                    print()
                    print("  1. use     - leave it as it stands")
                    print("  2. revise  - redefine key and invariants")
                    print()
                    if choose_from(["use", "revise"], "Choose") == "use":
                        print()
                        print("Nothing to change. Run it with:")
                        print()
                        print("  python scripts/etl_validator.py "
                              "--mapping {}".format(mapping_name))
                        print()
                        return 0

                key_columns = choose_key_columns(
                    cursor, mapping, target_columns, rules, current_key)
                checks = collect_aggregate_checks(cursor, current_checks)

                print()
                print("=" * 68)
                print("VALIDATION SUMMARY: {}".format(mapping_name))
                print("=" * 68)
                print("Identifying key:")
                for column in key_columns:
                    print("  - {}".format(column))
                print()
                if checks:
                    print("Cross-table invariants:")
                    for check in checks:
                        print("  {}".format(check["check_label"]))
                        print("    {}  =  {}".format(
                            describe_aggregate_side(
                                check["left_function"], check["left_schema"],
                                check["left_table"], check["left_column"]),
                            describe_aggregate_side(
                                check["right_function"], check["right_schema"],
                                check["right_table"], check["right_column"])))
                else:
                    print("No cross-table invariants.")
                    print("Row counts and column totals are derived from the")
                    print("mapping and run regardless.")

                print()
                if not prompt_yes_no("Save this configuration?", default=True):
                    print("Cancelled. Nothing was written.")
                    return 1

                replace_validation_key(
                    cursor, config.meta_schema,
                    mapping["mapping_set_id"], key_columns)
                replace_aggregate_checks(
                    cursor, config.meta_schema,
                    mapping["mapping_set_id"], checks)
    finally:
        connection.close()

    print()
    print("Validation configuration saved for '{}'.".format(mapping_name))
    print("Next: python scripts/etl_validator.py --mapping {}".format(
        mapping_name))
    return 0


def show_available(cursor, config) -> None:
    mappings = list_mapping_sets(cursor, config.meta_schema)
    if not mappings:
        print("The registry has no mappings. "
              "Run python scripts/map_wizard.py first.")
        return
    print("Available mappings:")
    for item in mappings:
        print("  {}  ({}.{} -> {}.{})".format(
            item["mapping_name"], item["source_schema"], item["source_table"],
            item["target_schema"], item["target_table"]))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record how a mapping's output should be validated."
    )
    parser.add_argument("--mapping", "-m", help="name of the mapping set")
    parser.add_argument("--list", action="store_true",
                        help="list registered mappings and exit")
    args = parser.parse_args()

    config = load_config()

    if args.list or not args.mapping:
        connection = config.connect()
        try:
            with connection.cursor() as cursor:
                show_available(cursor, config)
        finally:
            connection.close()
        sys.exit(0 if args.list else 2)

    sys.exit(run(config, args.mapping))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled. Nothing was written.")
    except ValueError as exc:
        sys.exit("ERROR: {}".format(exc))
    except psycopg2.Error as exc:
        sys.exit("DATABASE ERROR: {}".format(str(exc).strip()))
