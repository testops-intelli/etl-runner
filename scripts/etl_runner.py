"""ETL runner: move a staging table into a production table.

Invoked with a mapping name from the registry:

    python scripts/etl_runner.py --mapping holdings_to_holdings

Behaviour that matters:

  All-or-nothing. The whole run is one transaction. A row that cannot be
  transformed does not stop the run; it is recorded and processing continues so
  that EVERY failing row is reported in one pass. If any row failed, the
  transaction is rolled back and production is left untouched. A half-migrated
  target is worse than an unmigrated one.

  Rows are processed individually inside SAVEPOINTs so a database rejection can
  be attributed to the row that caused it. That is a deliberate trade of
  throughput for precise failure attribution, appropriate for migration
  rehearsal. Set-based insertion is the extension point for volume work.

  Success here means every source row was inserted with no failures. It is a
  completeness check, not a correctness check. Semantic and numerical integrity
  validation is a separate concern and is not performed by this script.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from etl.config import load_config
from etl.naming import quote_ident
from etl.registry import (
    get_column_rules,
    get_discarded_columns,
    get_mapping_set,
    list_mapping_sets,
    log_run,
)
from etl.transform import (
    RowError,
    build_lookup_caches,
    get_stage_columns,
    get_target_columns,
    table_exists,
    transform_row,
    validate_rules,
)

MAX_REPORTED_FAILURES = 25


def fetch_source_rows(cursor, mapping, stage_columns):
    column_list = ", ".join(quote_ident(name) for name in stage_columns)
    sql = "SELECT {} FROM {}.{}".format(
        column_list,
        quote_ident(mapping["source_schema"]),
        quote_ident(mapping["source_table"]),
    )
    if mapping["row_filter"]:
        sql += " WHERE {}".format(mapping["row_filter"])
    sql += ";"
    cursor.execute(sql)
    return [dict(zip(stage_columns, row)) for row in cursor.fetchall()]


def run(config, mapping_name: str) -> int:
    started_at = dt.datetime.now()
    connection = config.connect()
    connection.autocommit = False

    inserted = 0
    failures = []

    try:
        with connection.cursor() as cursor:
            mapping = get_mapping_set(cursor, config.meta_schema, mapping_name)
            if mapping is None:
                available = list_mapping_sets(cursor, config.meta_schema)
                print("ERROR: no mapping named '{}' in the registry.".format(
                    mapping_name))
                if available:
                    print("Available mappings:")
                    for item in available:
                        print("  {}  ({}.{} -> {}.{})".format(
                            item["mapping_name"], item["source_schema"],
                            item["source_table"], item["target_schema"],
                            item["target_table"]))
                else:
                    print("The registry has no mappings. "
                          "Run python scripts/map_wizard.py first.")
                return 2

            source_ref = "{}.{}".format(
                mapping["source_schema"], mapping["source_table"])
            target_ref = "{}.{}".format(
                mapping["target_schema"], mapping["target_table"])

            print("=" * 68)
            print("ETL RUN: {}".format(mapping_name))
            print("=" * 68)
            print("Source : {}".format(source_ref))
            print("Target : {}".format(target_ref))
            if mapping["row_filter"]:
                print("Filter : {}".format(mapping["row_filter"]))
            print()

            for schema, table, label in (
                (mapping["source_schema"], mapping["source_table"], "staging"),
                (mapping["target_schema"], mapping["target_table"], "target"),
            ):
                if not table_exists(cursor, schema, table):
                    print("ERROR: {} table {}.{} does not exist.".format(
                        label, schema, table))
                    return 2

            rules = get_column_rules(
                cursor, config.meta_schema, mapping["mapping_set_id"])
            if not rules:
                print("ERROR: mapping '{}' has no column rules.".format(
                    mapping_name))
                return 2

            stage_columns = get_stage_columns(
                cursor, mapping["source_schema"], mapping["source_table"])
            target_columns = get_target_columns(
                cursor, mapping["target_schema"], mapping["target_table"])
            target_types = {c["name"]: c["data_type"] for c in target_columns}

            problems = validate_rules(
                rules, stage_columns, [c["name"] for c in target_columns])
            if problems:
                print("ERROR: the mapping does not match the current schema.")
                for problem in problems:
                    print("  - {}".format(problem))
                print()
                print("Re-run python scripts/map_wizard.py to rebuild it.")
                return 2

            discarded = get_discarded_columns(
                cursor, config.meta_schema, mapping["mapping_set_id"])
            if discarded:
                print("Source columns discarded by this mapping: {}".format(
                    ", ".join(discarded)))
                print()

            lookup_caches = build_lookup_caches(cursor, rules)
            source_rows = fetch_source_rows(cursor, mapping, stage_columns)
            total = len(source_rows)
            print("Source rows to process: {}".format(total))
            print()

            insert_columns = [rule["target_column"] for rule in rules]
            insert_sql = "INSERT INTO {}.{} ({}) VALUES ({});".format(
                quote_ident(mapping["target_schema"]),
                quote_ident(mapping["target_table"]),
                ", ".join(quote_ident(name) for name in insert_columns),
                ", ".join(["%s"] * len(insert_columns)),
            )

            for position, source_row in enumerate(source_rows, start=1):
                try:
                    values = transform_row(
                        rules, target_types, source_row, lookup_caches)
                except RowError as exc:
                    failures.append({
                        "row": position,
                        "column": exc.target_column,
                        "reason": exc.reason,
                    })
                    continue

                cursor.execute("SAVEPOINT row_savepoint;")
                try:
                    cursor.execute(
                        insert_sql,
                        [values[name] for name in insert_columns],
                    )
                    cursor.execute("RELEASE SAVEPOINT row_savepoint;")
                    inserted += 1
                except psycopg2.Error as exc:
                    cursor.execute("ROLLBACK TO SAVEPOINT row_savepoint;")
                    failures.append({
                        "row": position,
                        "column": "(database)",
                        "reason": str(exc).strip().splitlines()[0],
                    })

            status = "PASS" if (failures == [] and inserted == total) else "FAIL"

            if status == "PASS":
                log_run(
                    cursor, config.meta_schema,
                    mapping_name=mapping_name, source_rows=total,
                    inserted_rows=inserted, failed_rows=0,
                    status=status, started_at=started_at,
                    message="all rows inserted",
                )
                connection.commit()
            else:
                connection.rollback()
                with connection.cursor() as log_cursor:
                    log_run(
                        log_cursor, config.meta_schema,
                        mapping_name=mapping_name, source_rows=total,
                        inserted_rows=0, failed_rows=len(failures),
                        status=status, started_at=started_at,
                        message="rolled back; {} row(s) failed".format(
                            len(failures)),
                    )
                connection.commit()

            print_report(status, total, inserted, failures, target_ref)
            return 0 if status == "PASS" else 1

    finally:
        connection.close()


def print_report(status, total, inserted, failures, target_ref) -> None:
    print("-" * 68)
    if status == "PASS":
        print("ETL RESULT: PASS")
        print("Rows inserted: {}/{}".format(inserted, total))
        print("Committed to {}.".format(target_ref))
        print()
        print("This confirms completeness only: every source row was inserted")
        print("with no failures. It does not confirm that the values are")
        print("semantically correct. Run the validator for that.")
    else:
        print("ETL RESULT: FAIL")
        print("Rows that would have inserted: {}/{}".format(inserted, total))
        print("Rows failed: {}".format(len(failures)))
        print()
        print("The transaction was rolled back. {} is unchanged.".format(
            target_ref))
        print()
        print("Failing rows:")
        for failure in failures[:MAX_REPORTED_FAILURES]:
            print("  row {:<6} column {:<20} {}".format(
                failure["row"], failure["column"], failure["reason"]))
        if len(failures) > MAX_REPORTED_FAILURES:
            print("  ... and {} more.".format(
                len(failures) - MAX_REPORTED_FAILURES))
    print("-" * 68)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a registered ETL mapping from staging to production."
    )
    parser.add_argument(
        "--mapping", "-m", required=False,
        help="name of the mapping set to run",
    )
    parser.add_argument(
        "--list", action="store_true", help="list registered mappings and exit"
    )
    args = parser.parse_args()

    config = load_config()

    if args.list or not args.mapping:
        connection = config.connect()
        try:
            with connection.cursor() as cursor:
                mappings = list_mapping_sets(cursor, config.meta_schema)
        finally:
            connection.close()
        if not mappings:
            print("No mappings registered. Run python scripts/map_wizard.py")
            sys.exit(0 if args.list else 2)
        print("Registered mappings:")
        for item in mappings:
            print("  {}  ({}.{} -> {}.{})".format(
                item["mapping_name"], item["source_schema"],
                item["source_table"], item["target_schema"],
                item["target_table"]))
        sys.exit(0 if args.list else 2)

    sys.exit(run(config, args.mapping))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except psycopg2.Error as exc:
        sys.exit("DATABASE ERROR: {}".format(str(exc).strip()))
