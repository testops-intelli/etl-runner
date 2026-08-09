"""ETL validator: check that what production holds is what the mapping says.

    python scripts/etl_validator.py --mapping holdings_to_prod

The runner answers completeness - every source row was inserted. This answers
correctness, in three layers:

  Row level. Every target value is re-derived from the staging row and the
  registry's rules, then matched against production on the identifying key
  recorded by the validation wizard. Missing rows, orphan rows, duplicate keys
  and per-column value mismatches are reported separately, because they mean
  different things.

  Derived aggregates. Row counts, and a total for every numeric column carried
  across by a DIRECT rule. No configuration: the registry already knows enough.

  Declared invariants. The cross-table checks recorded by the wizard. These are
  the only ones that can catch a mapping which is wrong rather than incomplete,
  since a re-derivation uses the same rule the runner used and will agree with
  it either way.

This script never writes to production. Its only write is a row in
etl_meta.validation_run recording that the validation happened.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from etl.config import load_config
from etl.evidence import write_workbook
from etl.naming import quote_ident
from etl.registry import (
    get_aggregate_checks,
    get_column_rules,
    get_discarded_columns,
    get_mapping_set,
    get_validation_key,
    list_mapping_sets,
    log_validation,
)
from etl.transform import (
    build_lookup_caches,
    get_stage_columns,
    get_target_columns,
    table_exists,
    validate_rules,
)
from etl.validation import (
    column_coverage,
    compare_rows,
    declared_aggregates,
    derive_expected,
    derived_aggregates,
    fetch_target_rows,
    render,
)

MAX_REPORTED = 25


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


def run(config, mapping_name: str, workbook_dir: Path) -> int:
    started_at = dt.datetime.now()
    connection = config.connect()
    connection.autocommit = False

    try:
        with connection.cursor() as cursor:
            mapping = get_mapping_set(cursor, config.meta_schema, mapping_name)
            if mapping is None:
                print("ERROR: no mapping named '{}' in the registry.".format(
                    mapping_name))
                show_available(cursor, config)
                return 2

            source_ref = "{}.{}".format(
                mapping["source_schema"], mapping["source_table"])
            target_ref = "{}.{}".format(
                mapping["target_schema"], mapping["target_table"])

            print("=" * 68)
            print("ETL VALIDATION: {}".format(mapping_name))
            print("=" * 68)
            print("Source : {}".format(source_ref))
            print("Target : {}".format(target_ref))
            if mapping["row_filter"]:
                print("Filter : {}".format(mapping["row_filter"]))

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

            key_columns = get_validation_key(
                cursor, config.meta_schema, mapping["mapping_set_id"])
            if not key_columns:
                print()
                print("ERROR: no identifying key recorded for this mapping.")
                print("A row cannot be matched without one. Record it with:")
                print()
                print("  python scripts/validate_wizard.py --mapping {}".format(
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
                return 2

            print("Key    : {}".format(" + ".join(key_columns)))
            discarded = get_discarded_columns(
                cursor, config.meta_schema, mapping["mapping_set_id"])
            if discarded:
                print("Source columns not carried across, so not validated: "
                      "{}".format(", ".join(discarded)))
            print()

            lookup_caches = build_lookup_caches(cursor, rules)
            source_rows = fetch_source_rows(cursor, mapping, stage_columns)
            expected, derivation_errors = derive_expected(
                rules, target_types, source_rows, lookup_caches)

            mapped_columns = [rule["target_column"] for rule in rules]
            actual = fetch_target_rows(
                cursor, mapping["target_schema"], mapping["target_table"],
                mapped_columns)

            comparison = compare_rows(
                expected, actual, key_columns, mapped_columns)

            auto_checks = derived_aggregates(
                rules, target_columns, expected, cursor,
                mapping["target_schema"], mapping["target_table"])
            declared_checks = declared_aggregates(
                cursor, get_aggregate_checks(
                    cursor, config.meta_schema, mapping["mapping_set_id"]))

            coverage = column_coverage(target_columns, rules, key_columns)

            failed_aggregates = sum(
                1 for check in auto_checks + declared_checks
                if check["status"] == "FAIL")
            clean = (
                not comparison["missing"] and not comparison["orphans"]
                and not comparison["duplicates"]
                and not comparison["mismatches"]
                and not derivation_errors and failed_aggregates == 0
            )
            status = "PASS" if clean else "FAIL"

            print_report(status, comparison, derivation_errors,
                         auto_checks, declared_checks, coverage,
                         len(source_rows), len(actual))

            workbook_path = workbook_dir / "validation_{}_{}.xlsx".format(
                mapping_name, started_at.strftime("%Y%m%d_%H%M%S"))
            write_workbook(
                workbook_path, mapping=mapping, status=status,
                started_at=started_at, comparison=comparison,
                derivation_errors=derivation_errors,
                auto_aggregates=auto_checks,
                declared_aggregates=declared_checks, coverage=coverage,
                key_columns=key_columns, source_rows=len(source_rows),
                target_rows=len(actual))

            log_validation(
                cursor, config.meta_schema,
                mapping_name=mapping_name,
                source_rows=len(source_rows), target_rows=len(actual),
                matched_rows=comparison["matched"],
                missing_rows=len(comparison["missing"]),
                orphan_rows=len(comparison["orphans"]),
                duplicate_keys=len(comparison["duplicates"]),
                mismatched_values=len(comparison["mismatches"]),
                aggregates_run=len(auto_checks) + len(declared_checks),
                aggregates_failed=failed_aggregates,
                status=status, started_at=started_at,
                message="evidence: {}".format(workbook_path.name),
            )
            connection.commit()

            print()
            print("Evidence workbook: {}".format(workbook_path))
            print("-" * 68)
            return 0 if status == "PASS" else 1
    finally:
        connection.close()


def print_report(status, comparison, derivation_errors,
                 auto_checks, declared_checks, coverage,
                 source_rows, target_rows) -> None:
    print("-" * 68)
    print("ROW COMPARISON")
    print("-" * 68)
    print("  Source rows            {}".format(source_rows))
    print("  Target rows            {}".format(target_rows))
    print("  Matched                {}".format(comparison["matched"]))
    print("  Missing from target    {}".format(len(comparison["missing"])))
    print("  In target, no source   {}".format(len(comparison["orphans"])))
    print("  Duplicate keys         {}".format(len(comparison["duplicates"])))
    print("  Value mismatches       {}".format(len(comparison["mismatches"])))

    if derivation_errors:
        print()
        print("Rows the mapping could not derive at all:")
        for item in derivation_errors[:MAX_REPORTED]:
            print("  row {:<6} column {:<20} {}".format(
                item["row"], item["column"], item["reason"]))
        _more(len(derivation_errors))

    if comparison["mismatches"]:
        print()
        print("Value mismatches:")
        for item in comparison["mismatches"][:MAX_REPORTED]:
            print("  row {:<6} {:<20} expected {} but found {}".format(
                item["source_row"], item["column"],
                item["expected"], item["actual"]))
        _more(len(comparison["mismatches"]))

    if comparison["missing"]:
        print()
        print("Rows missing from the target:")
        for item in comparison["missing"][:MAX_REPORTED]:
            print("  row {:<6} {}".format(item["source_row"], item["key"]))
        _more(len(comparison["missing"]))

    if comparison["orphans"]:
        print()
        print("Target rows with no source row:")
        for item in comparison["orphans"][:MAX_REPORTED]:
            print("  {}".format(item["key"]))
        _more(len(comparison["orphans"]))

    if comparison["duplicates"]:
        print()
        print("Keys appearing more than once in the target:")
        for item in comparison["duplicates"][:MAX_REPORTED]:
            print("  {}  expected {} row(s), found {}".format(
                item["key"], item["expected_count"], item["actual_count"]))
        _more(len(comparison["duplicates"]))

    print()
    print("-" * 68)
    print("AGGREGATES")
    print("-" * 68)
    for label, group in (("derived from the mapping", auto_checks),
                         ("declared invariants", declared_checks)):
        if not group:
            continue
        print("  {}:".format(label))
        for check in group:
            print("    [{}] {:<28} {} vs {}{}".format(
                check["status"], check["label"],
                render(check["left_value"]), render(check["right_value"]),
                "" if check["status"] == "PASS"
                else "   variance {}".format(render(check["variance"]))))
    if not declared_checks:
        print("  No cross-table invariants are configured. Row-level checks")
        print("  and column totals confirm the transfer was faithful to the")
        print("  mapping; they cannot confirm the mapping itself is right.")

    print()
    print("-" * 68)
    print("COLUMN COVERAGE")
    print("-" * 68)
    for item in coverage:
        print("  {:<26} {}".format(item["column"], item["validation"]))

    print()
    print("=" * 68)
    print("VALIDATION RESULT: {}".format(status))
    print("=" * 68)


def _more(total: int) -> None:
    if total > MAX_REPORTED:
        print("  ... and {} more.".format(total - MAX_REPORTED))


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
        description="Validate a migrated table against its mapping."
    )
    parser.add_argument("--mapping", "-m", help="name of the mapping set")
    parser.add_argument("--list", action="store_true",
                        help="list registered mappings and exit")
    parser.add_argument(
        "--evidence-dir", default="evidence",
        help="directory for the xlsx evidence workbook (default: evidence)")
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

    workbook_dir = Path(args.evidence_dir)
    if not workbook_dir.is_absolute():
        workbook_dir = Path(__file__).resolve().parent.parent / workbook_dir

    sys.exit(run(config, args.mapping, workbook_dir))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except psycopg2.Error as exc:
        sys.exit("DATABASE ERROR: {}".format(str(exc).strip()))
