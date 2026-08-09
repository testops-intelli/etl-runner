"""Mapping wizard: define how a staging table becomes a production table.

The wizard is driven by the TARGET table, not the source. Production is the
contract; every target column must be accounted for, and the wizard walks them
one at a time. Source columns nobody claims are then listed and recorded
explicitly as discarded, so a dropped column is a decision on the record rather
than an omission nobody noticed.

The result is rows in the metadata registry. Nothing is written into Python or
SQL source, which is what allows one runner to serve any table pair.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from etl.config import load_config
from etl.registry import (
    derive_discarded_columns,
    describe_rule,
    find_mappings_for_pair,
    get_column_rules,
    insert_column_rule,
    insert_discarded_columns,
    insert_value_map,
    rule_to_payload,
    upsert_mapping_set,
)
from etl.transform import (
    get_stage_columns,
    get_target_columns,
    table_exists,
)

RULE_MENU = (
    ("DIRECT", "copy a source column across, cast to the target type"),
    ("VALUE_MAP", "translate coded values, e.g. ACTIVE -> A"),
    ("LOOKUP", "resolve a code or name to a key in another table"),
    ("CONSTANT", "same fixed value for every row"),
    ("EXPRESSION", "build from source columns, e.g. D_{action_id}"),
    ("NULL", "leave deliberately empty"),
)


class GoBack(Exception):
    """Step back to the PREVIOUS target column."""


class Redo(Exception):
    """Restart the CURRENT target column from its first question."""


# Navigation words are spelled out in full and never abbreviated. Value maps
# exist precisely to produce single-letter codes -- A, C, D, F, I, P, R, S --
# so a one-letter navigation shortcut would collide with legitimate input.
BACK_TOKENS = ("back",)
REDO_TOKENS = ("redo", "restart")

NAV_HINT = ("  ('redo' restarts this column, 'back' goes to the previous one)")


def _check_navigation(answer: str, allow_nav: bool) -> None:
    if not allow_nav:
        return
    token = answer.strip().lower()
    if token in REDO_TOKENS:
        raise Redo()
    if token in BACK_TOKENS:
        raise GoBack()


def prompt(message: str, default: str = None, allow_back: bool = False) -> str:
    suffix = " [{}]".format(default) if default else ""
    while True:
        answer = input("{}{}: ".format(message, suffix)).strip()
        _check_navigation(answer, allow_back)
        if answer:
            return answer
        if default is not None:
            return default
        print("  An answer is required.")


def prompt_optional(message: str, allow_back: bool = False) -> str:
    answer = input("{}: ".format(message)).strip()
    _check_navigation(answer, allow_back)
    return answer


def prompt_yes_no(message: str, default: bool = True,
                  allow_back: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        answer = input("{} ({}): ".format(message, hint)).strip().lower()
        _check_navigation(answer, allow_back)
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n{}.".format(
            ", or 'redo' to restart this column" if allow_back else ""))


def choose_from(items, message: str, allow_back: bool = False):
    for index, item in enumerate(items, start=1):
        print("  {}. {}".format(index, item))
    while True:
        answer = prompt(message, allow_back=allow_back)
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        if answer in items:
            return answer
        print("  Not a valid choice. Enter a number from 1 to {}, "
              "or the name itself.".format(len(items)))


def list_tables(cursor, schema: str):
    cursor.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name;
        """,
        (schema,),
    )
    return [row[0] for row in cursor.fetchall()]


def distinct_source_values(cursor, schema: str, table: str, column: str, limit=40):
    cursor.execute(
        'SELECT DISTINCT "{}" FROM "{}"."{}" '
        'WHERE "{}" IS NOT NULL AND "{}" <> \'\' '
        'ORDER BY 1 LIMIT %s;'.format(column, schema, table, column, column),
        (limit,),
    )
    return [row[0] for row in cursor.fetchall()]


def build_rule(cursor, config, stage_table, stage_columns, target_column,
               current=None):
    """Interactively construct one column rule for one target column.

    When `current` is supplied the column already has a rule from a previous
    run. It is displayed and can be kept with a bare Enter, so revising an
    existing mapping only costs keystrokes on the columns actually changing.
    """
    print()
    print("-" * 68)
    print("TARGET COLUMN: {}  ({}{})".format(
        target_column["name"],
        target_column["data_type"],
        "" if target_column["nullable"] else ", NOT NULL",
    ))
    print("-" * 68)
    if current:
        print("Current rule: {}".format(describe_rule(current)))
        print()
    print("Rule types:")
    for index, (name, description) in enumerate(RULE_MENU, start=1):
        print("  {}. {:<11} {}".format(index, name, description))
    print(NAV_HINT)

    # No default unless a current rule exists. Without one, pressing Enter used
    # to silently select DIRECT, which made a wrong rule the cheapest possible
    # keystroke. With one, Enter means "keep what is already there", which is
    # displayed above and is never a guess.
    while True:
        if current:
            answer = prompt(
                "Rule type (1-{}, Enter to keep current)".format(
                    len(RULE_MENU)),
                default="__keep__", allow_back=True)
            if answer == "__keep__":
                return rule_to_payload(current), dict(
                    current.get("value_map") or {})
        else:
            answer = prompt("Rule type (1-{})".format(len(RULE_MENU)),
                            allow_back=True)
        if answer.isdigit() and 1 <= int(answer) <= len(RULE_MENU):
            rule_type = RULE_MENU[int(answer) - 1][0]
            break
        if answer.upper() in [name for name, _ in RULE_MENU]:
            rule_type = answer.upper()
            break
        print("  Not a valid rule type. Enter a number from 1 to {}, "
              "or the name itself.".format(len(RULE_MENU)))

    rule = {"target_column": target_column["name"], "rule_type": rule_type}
    value_map = {}

    if rule_type == "NULL":
        return rule, value_map

    if rule_type == "CONSTANT":
        rule["constant_value"] = prompt(
            "Constant value for every row (target type {})".format(
                target_column["data_type"]),
            allow_back=True,
        )
        return rule, value_map

    if rule_type == "EXPRESSION":
        print()
        print("Reference source columns in braces, e.g.  D_{action_id}")
        print("Available: {}".format(", ".join(stage_columns)))
        rule["expression_template"] = prompt(
            "Expression template", allow_back=True)
        return rule, value_map

    # The remaining types all read a source column.
    print()
    print("Source columns in {}.{}:".format(config.stage_schema, stage_table))
    source_column = choose_from(
        stage_columns, "Source column", allow_back=True)
    rule["source_column"] = source_column

    if rule_type == "DIRECT":
        return rule, value_map

    if rule_type == "VALUE_MAP":
        values = distinct_source_values(
            cursor, config.stage_schema, stage_table, source_column
        )
        previous_pairs = {}
        if current and current.get("rule_type") == "VALUE_MAP":
            previous_pairs = dict(current.get("value_map") or {})
        previous = {str(k).strip().upper(): v
                    for k, v in previous_pairs.items()}

        # A translation already in the map whose code does not appear in the
        # extract currently staged must still be offered, or revising against a
        # small batch would silently discard every code that batch happens not
        # to contain, and a later batch carrying one would fail on a value that
        # was mapped correctly all along.
        present = {str(value).strip().upper() for value in values}
        carried = [key for key in previous_pairs
                   if str(key).strip().upper() not in present]

        print()
        print("Distinct values found in '{}' ({}):".format(
            source_column, len(values)))
        if previous:
            print("Values already mapped show their current target in")
            print("brackets; press Enter to keep it. A value with no target")
            print("shown is new since the mapping was built.")
        print("Leave blank to leave a value unmapped, which will fail any row")
        print("carrying it unless a fallback is configured below.")
        for value in values:
            existing = previous.get(str(value).strip().upper())
            label = "  {!r} ->".format(value)
            if existing is not None:
                label += " [{}]".format(existing)
            # Navigation is disabled here: every keystroke is a target value,
            # and a target value could legitimately be any word at all.
            translated = prompt_optional(label)
            if translated:
                value_map[value] = translated
            elif existing is not None:
                value_map[value] = existing

        if carried:
            print()
            print("Already mapped, but not present in the data currently")
            print("staged. Press Enter to keep, or blank out to remove:")
            for key in carried:
                translated = prompt_optional(
                    "  {!r} -> [{}]".format(key, previous_pairs[key]))
                value_map[key] = translated or previous_pairs[key]

        print()
        print("('redo' at the next question restarts this column.)")
        print()
        print("A fallback catches values that appear later but are not in the")
        print("map above. Without one, an unexpected value fails the row and")
        print("the run, which is how an unnoticed mapping gap surfaces.")
        if prompt_yes_no("Configure a fallback value?", default=False,
                         allow_back=True):
            rule["allow_unmapped"] = True
            rule["unmapped_default"] = prompt_optional(
                "  Fallback value (blank for NULL)") or None
        return rule, value_map

    if rule_type == "LOOKUP":
        print()
        candidate_schemas = [config.ref_schema, config.prod_schema,
                             config.stage_schema]
        print("Schema holding the lookup table:")
        lookup_schema = choose_from(
            candidate_schemas, "Lookup schema", allow_back=True)
        tables = list_tables(cursor, lookup_schema)
        if not tables:
            print()
            print("  Schema '{}' contains no tables, so there is nothing to "
                  "resolve against.".format(lookup_schema))
            print("  Restarting this column so you can choose again.")
            raise Redo()
        print()
        print("Lookup table:")
        lookup_table = choose_from(tables, "Lookup table", allow_back=True)
        lookup_columns = [
            column["name"]
            for column in get_target_columns(cursor, lookup_schema, lookup_table)
        ]
        print()
        print("Column in {}.{} that '{}' should be matched against:".format(
            lookup_schema, lookup_table, source_column))
        match_column = choose_from(
            lookup_columns, "Match column", allow_back=True)
        print()
        print("Column whose value is written into '{}':".format(
            target_column["name"]))
        return_column = choose_from(
            lookup_columns, "Return column", allow_back=True)

        rule.update({
            "lookup_schema": lookup_schema,
            "lookup_table": lookup_table,
            "lookup_match_column": match_column,
            "lookup_return_column": return_column,
        })

        print()
        if prompt_yes_no(
            "Allow a fallback when no match is found?", default=False,
            allow_back=True,
        ):
            rule["allow_unmapped"] = True
            rule["unmapped_default"] = prompt_optional(
                "  Fallback value (blank for NULL)") or None
        return rule, value_map

    raise ValueError("unhandled rule type {}".format(rule_type))


def main() -> None:
    config = load_config()

    print("=" * 68)
    print("MAPPING WIZARD - staging table to production table")
    print("=" * 68)

    connection = config.connect()
    try:
        with connection:
            with connection.cursor() as cursor:
                stage_tables = list_tables(cursor, config.stage_schema)
                if not stage_tables:
                    sys.exit(
                        "No staging tables found in schema '{}'.\n"
                        "Run python scripts/ingest.py first.".format(
                            config.stage_schema)
                    )
                print()
                print("Staging tables in '{}':".format(config.stage_schema))
                stage_table = choose_from(stage_tables, "Source staging table")

                prod_tables = list_tables(cursor, config.prod_schema)
                if not prod_tables:
                    sys.exit(
                        "No production tables found in schema '{}'.\n"
                        "Run python scripts/create_env.py first.".format(
                            config.prod_schema)
                    )
                print()
                print("Production tables in '{}':".format(config.prod_schema))
                target_table = choose_from(prod_tables, "Target production table")

                if not table_exists(cursor, config.stage_schema, stage_table):
                    sys.exit("Staging table disappeared: {}.{}".format(
                        config.stage_schema, stage_table))

                stage_columns = get_stage_columns(
                    cursor, config.stage_schema, stage_table)
                target_columns = get_target_columns(
                    cursor, config.prod_schema, target_table)

                # A mapping already registered for this pair can be reused as
                # it stands. Re-ingesting a later batch into the same staging
                # table needs no wizard at all, only the runner.
                existing = find_mappings_for_pair(
                    cursor, config.meta_schema,
                    config.stage_schema, stage_table,
                    config.prod_schema, target_table)

                existing_rules = None
                mapping_name = None
                row_filter = None

                if existing:
                    print()
                    print("=" * 68)
                    print("An ETL mapping is already registered for "
                          "{}.{} -> {}.{}".format(
                              config.stage_schema, stage_table,
                              config.prod_schema, target_table))
                    print("=" * 68)
                    for item in existing:
                        print("  {}  (created {}{})".format(
                            item["mapping_name"],
                            item["created_at"].strftime("%Y-%m-%d %H:%M"),
                            ", filter: " + item["row_filter"]
                            if item["row_filter"] else ""))
                    print()
                    print("  1. Use it as it stands - nothing to define, just")
                    print("     run the ETL. Correct for a later batch of the")
                    print("     same extract.")
                    print("  2. Revise it - walk the columns with the current")
                    print("     rules prefilled, changing only what differs.")
                    print("  3. Define a new mapping under a different name.")
                    print()
                    action = choose_from(
                        ["use", "revise", "new"], "Choose")

                    if action == "use":
                        chosen = existing[0]["mapping_name"]
                        if len(existing) > 1:
                            print()
                            chosen = choose_from(
                                [i["mapping_name"] for i in existing],
                                "Which mapping")
                        print()
                        print("Nothing to change. Run it with:")
                        print()
                        print("  python scripts/etl_runner.py --mapping {}"
                              .format(chosen))
                        print()
                        return

                    if action == "revise":
                        chosen = existing[0]
                        if len(existing) > 1:
                            print()
                            name = choose_from(
                                [i["mapping_name"] for i in existing],
                                "Which mapping")
                            chosen = next(i for i in existing
                                          if i["mapping_name"] == name)
                        mapping_name = chosen["mapping_name"]
                        row_filter = chosen["row_filter"]
                        existing_rules = {
                            rule["target_column"]: rule
                            for rule in get_column_rules(
                                cursor, config.meta_schema,
                                chosen["mapping_set_id"])
                        }
                        print()
                        print("Revising '{}'. Press Enter at any column to "
                              "keep its current rule.".format(mapping_name))

                if mapping_name is None:
                    default_name = "{}_to_{}".format(stage_table, target_table)
                    print()
                    print("A mapping set is identified by name. The ETL runner")
                    print("is invoked with this name, so one staging table can")
                    print("feed several targets under different mapping names.")
                    mapping_name = prompt("Mapping name", default_name)

                    print()
                    print("An optional row filter restricts which staging rows")
                    print("this mapping consumes, e.g. action_type = 'DIVIDEND'")
                    print("Leave blank to consume every row.")
                    row_filter = prompt_optional(
                        "Row filter (SQL, no WHERE)") or None

                print()
                print("Target table {}.{} has {} columns.".format(
                    config.prod_schema, target_table, len(target_columns)))
                auto_columns = [
                    c["name"] for c in target_columns if c["is_auto_generated"]
                ]
                if auto_columns:
                    print("Database-generated columns will be skipped: {}".format(
                        ", ".join(auto_columns)))

                mappable = [
                    column for column in target_columns
                    if not column["is_auto_generated"]
                ]
                rules = []
                value_maps = {}
                position = 0
                while position < len(mappable):
                    target_column = mappable[position]
                    print()
                    print("[column {} of {}]".format(
                        position + 1, len(mappable)))
                    try:
                        rule, value_map = build_rule(
                            cursor, config, stage_table, stage_columns,
                            target_column,
                            current=(existing_rules or {}).get(
                                target_column["name"]),
                        )
                    except Redo:
                        print()
                        print("  Restarting '{}'.".format(
                            target_column["name"]))
                        continue
                    except GoBack:
                        if position == 0:
                            print()
                            print("  Already at the first column. Use 'redo' "
                                  "to restart this one.")
                            continue
                        position -= 1
                        # The earlier answer is left in place and will be
                        # overwritten when the column is answered again.
                        print()
                        print("  Stepping back to '{}'.".format(
                            mappable[position]["name"]))
                        continue
                    except KeyboardInterrupt:
                        print()
                        if rules:
                            print()
                            print("  {} column(s) already defined would be "
                                  "lost.".format(len(rules)))
                        if prompt_yes_no(
                            "  Abandon this mapping entirely?", default=False
                        ):
                            sys.exit("Cancelled. Nothing was written.")
                        print("  Continuing. Re-entering the current column.")
                        continue

                    # Overwrite rather than append, so stepping back and
                    # re-answering replaces the earlier answer.
                    if position < len(rules):
                        rules[position] = rule
                    else:
                        rules.append(rule)
                    if value_map:
                        value_maps[rule["target_column"]] = value_map
                    else:
                        value_maps.pop(rule["target_column"], None)
                    position += 1

                unconsumed = derive_discarded_columns(rules, stage_columns)

                print()
                print("=" * 68)
                print("MAPPING SUMMARY: {}".format(mapping_name))
                print("=" * 68)
                print("{}.{}  ->  {}.{}".format(
                    config.stage_schema, stage_table,
                    config.prod_schema, target_table))
                if row_filter:
                    print("Row filter: {}".format(row_filter))
                print()
                for rule in rules:
                    detail = ""
                    if rule["rule_type"] == "DIRECT":
                        detail = "from {}".format(rule["source_column"])
                    elif rule["rule_type"] == "CONSTANT":
                        detail = "= {!r}".format(rule["constant_value"])
                    elif rule["rule_type"] == "VALUE_MAP":
                        detail = "from {} ({} values mapped)".format(
                            rule["source_column"],
                            len(value_maps.get(rule["target_column"], {})))
                    elif rule["rule_type"] == "LOOKUP":
                        detail = "from {} via {}.{}.{}".format(
                            rule["source_column"], rule["lookup_schema"],
                            rule["lookup_table"], rule["lookup_match_column"])
                    elif rule["rule_type"] == "EXPRESSION":
                        detail = "= {!r}".format(rule["expression_template"])
                    print("  {:<22} {:<11} {}".format(
                        rule["target_column"], rule["rule_type"], detail))

                if unconsumed:
                    print()
                    print("Source columns not used by any rule:")
                    for column in unconsumed:
                        print("  - {}".format(column))
                    print("These will be recorded as DISCARD against this mapping.")

                print()
                if not prompt_yes_no("Save this mapping?", default=True):
                    sys.exit("Cancelled. Nothing was written.")

                mapping_set_id = upsert_mapping_set(
                    cursor, config.meta_schema, mapping_name,
                    config.stage_schema, stage_table,
                    config.prod_schema, target_table, row_filter,
                )
                for rule in rules:
                    rule_id = insert_column_rule(
                        cursor, config.meta_schema, mapping_set_id, rule)
                    pairs = value_maps.get(rule["target_column"])
                    if pairs:
                        insert_value_map(
                            cursor, config.meta_schema, rule_id, pairs)
                insert_discarded_columns(
                    cursor, config.meta_schema, mapping_set_id, unconsumed)
    finally:
        connection.close()

    print()
    print("Mapping '{}' saved to the registry.".format(mapping_name))
    print("Next: python scripts/etl_runner.py --mapping {}".format(mapping_name))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except ValueError as exc:
        sys.exit("ERROR: {}".format(exc))
    except psycopg2.Error as exc:
        sys.exit("DATABASE ERROR: {}".format(str(exc).strip()))
