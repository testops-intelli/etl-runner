"""The metadata registry: mapping rules, ingestion log, run log.

Everything the ETL runner needs in order to move a staging table into a target
table lives in these tables. No table name, column name, constant or code
mapping is written into Python or SQL source. That is the whole point: the same
runner moves any staging table into any target table, driven by rows here.
"""

from typing import Dict, List, Optional

from .naming import quote_ident

RULE_TYPES = (
    "DIRECT",      # copy source column, cast to the target column type
    "CONSTANT",    # fixed literal value for every row
    "VALUE_MAP",   # coded translation, e.g. ACTIVE -> A
    "LOOKUP",      # resolve a code/name to a surrogate key in another table
    "EXPRESSION",  # template built from source columns, e.g. D_{action_id}
    "NULL",        # deliberately left empty
)


def create_registry(cursor, meta_schema: str) -> None:
    """Create the metadata schema and its tables if they do not exist."""
    cursor.execute("CREATE SCHEMA IF NOT EXISTS {};".format(quote_ident(meta_schema)))
    schema = quote_ident(meta_schema)

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS {schema}.mapping_set (
            mapping_set_id   BIGSERIAL PRIMARY KEY,
            mapping_name     TEXT        NOT NULL UNIQUE,
            source_schema    TEXT        NOT NULL,
            source_table     TEXT        NOT NULL,
            target_schema    TEXT        NOT NULL,
            target_table     TEXT        NOT NULL,
            row_filter       TEXT,
            created_at       TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """.format(schema=schema)
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS {schema}.column_rule (
            rule_id              BIGSERIAL PRIMARY KEY,
            mapping_set_id       BIGINT      NOT NULL
                                 REFERENCES {schema}.mapping_set(mapping_set_id)
                                 ON DELETE CASCADE,
            target_column        TEXT        NOT NULL,
            rule_type            TEXT        NOT NULL
                                 CHECK (rule_type IN
                                 ('DIRECT','CONSTANT','VALUE_MAP','LOOKUP',
                                  'EXPRESSION','NULL')),
            source_column        TEXT,
            constant_value       TEXT,
            expression_template  TEXT,
            lookup_schema        TEXT,
            lookup_table         TEXT,
            lookup_match_column  TEXT,
            lookup_return_column TEXT,
            unmapped_default     TEXT,
            allow_unmapped       BOOLEAN     NOT NULL DEFAULT FALSE,
            UNIQUE (mapping_set_id, target_column)
        );
        """.format(schema=schema)
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS {schema}.value_map (
            value_map_id  BIGSERIAL PRIMARY KEY,
            rule_id       BIGINT NOT NULL
                          REFERENCES {schema}.column_rule(rule_id)
                          ON DELETE CASCADE,
            source_value  TEXT   NOT NULL,
            target_value  TEXT,
            UNIQUE (rule_id, source_value)
        );
        """.format(schema=schema)
    )

    # Source columns that intentionally go nowhere are recorded rather than
    # implied, so a discard is an auditable decision instead of an omission.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS {schema}.discarded_source_column (
            discard_id     BIGSERIAL PRIMARY KEY,
            mapping_set_id BIGINT NOT NULL
                           REFERENCES {schema}.mapping_set(mapping_set_id)
                           ON DELETE CASCADE,
            source_column  TEXT   NOT NULL,
            UNIQUE (mapping_set_id, source_column)
        );
        """.format(schema=schema)
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS {schema}.ingestion_log (
            ingestion_id   BIGSERIAL PRIMARY KEY,
            source_file    TEXT      NOT NULL,
            loaded_file    TEXT      NOT NULL,
            was_converted  BOOLEAN   NOT NULL,
            stage_schema   TEXT      NOT NULL,
            stage_table    TEXT      NOT NULL,
            column_count   INT       NOT NULL,
            row_count      BIGINT    NOT NULL,
            loaded_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """.format(schema=schema)
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS {schema}.etl_run (
            run_id         BIGSERIAL PRIMARY KEY,
            mapping_name   TEXT      NOT NULL,
            source_rows    BIGINT    NOT NULL,
            inserted_rows  BIGINT    NOT NULL,
            failed_rows    BIGINT    NOT NULL,
            status         TEXT      NOT NULL,
            started_at     TIMESTAMP NOT NULL,
            finished_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            message        TEXT
        );
        """.format(schema=schema)
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

def get_mapping_set(cursor, meta_schema: str, mapping_name: str) -> Optional[Dict]:
    cursor.execute(
        """
        SELECT mapping_set_id, mapping_name, source_schema, source_table,
               target_schema, target_table, row_filter
        FROM {}.mapping_set
        WHERE mapping_name = %s;
        """.format(quote_ident(meta_schema)),
        (mapping_name,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    keys = (
        "mapping_set_id", "mapping_name", "source_schema", "source_table",
        "target_schema", "target_table", "row_filter",
    )
    return dict(zip(keys, row))


def list_mapping_sets(cursor, meta_schema: str) -> List[Dict]:
    cursor.execute(
        """
        SELECT mapping_name, source_schema, source_table,
               target_schema, target_table
        FROM {}.mapping_set
        ORDER BY mapping_name;
        """.format(quote_ident(meta_schema))
    )
    keys = ("mapping_name", "source_schema", "source_table",
            "target_schema", "target_table")
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def find_mappings_for_pair(cursor, meta_schema: str, source_schema: str,
                          source_table: str, target_schema: str,
                          target_table: str) -> List[Dict]:
    """Mappings already registered for this exact staging/target pair."""
    cursor.execute(
        """
        SELECT mapping_set_id, mapping_name, row_filter, created_at
        FROM {}.mapping_set
        WHERE source_schema = %s AND source_table = %s
          AND target_schema = %s AND target_table = %s
        ORDER BY mapping_name;
        """.format(quote_ident(meta_schema)),
        (source_schema, source_table, target_schema, target_table),
    )
    keys = ("mapping_set_id", "mapping_name", "row_filter", "created_at")
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def find_mappings_for_source(cursor, meta_schema: str, source_schema: str,
                             source_table: str) -> List[Dict]:
    """Every mapping that consumes this staging table, whatever the target."""
    cursor.execute(
        """
        SELECT mapping_name, target_schema, target_table
        FROM {}.mapping_set
        WHERE source_schema = %s AND source_table = %s
        ORDER BY mapping_name;
        """.format(quote_ident(meta_schema)),
        (source_schema, source_table),
    )
    keys = ("mapping_name", "target_schema", "target_table")
    return [dict(zip(keys, row)) for row in cursor.fetchall()]


def rule_to_payload(rule: Dict) -> Dict:
    """Strip a rule read from the registry back to an insertable payload."""
    return {
        key: rule.get(key)
        for key in (
            "target_column", "rule_type", "source_column", "constant_value",
            "expression_template", "lookup_schema", "lookup_table",
            "lookup_match_column", "lookup_return_column",
            "unmapped_default", "allow_unmapped",
        )
    }


def describe_rule(rule: Dict, value_map_size: int = None) -> str:
    """One-line human description of a rule, used in prompts and summaries."""
    kind = rule["rule_type"]
    if kind == "DIRECT":
        return "DIRECT from {}".format(rule["source_column"])
    if kind == "CONSTANT":
        return "CONSTANT = {!r}".format(rule["constant_value"])
    if kind == "NULL":
        return "NULL (deliberately empty)"
    if kind == "EXPRESSION":
        return "EXPRESSION = {!r}".format(rule["expression_template"])
    if kind == "VALUE_MAP":
        size = value_map_size
        if size is None:
            size = len(rule.get("value_map") or {})
        return "VALUE_MAP from {} ({} values mapped)".format(
            rule["source_column"], size)
    if kind == "LOOKUP":
        return "LOOKUP from {} via {}.{}.{} -> {}".format(
            rule["source_column"], rule["lookup_schema"], rule["lookup_table"],
            rule["lookup_match_column"], rule["lookup_return_column"])
    return kind


def get_column_rules(cursor, meta_schema: str, mapping_set_id: int) -> List[Dict]:
    schema = quote_ident(meta_schema)
    cursor.execute(
        """
        SELECT rule_id, target_column, rule_type, source_column, constant_value,
               expression_template, lookup_schema, lookup_table,
               lookup_match_column, lookup_return_column,
               unmapped_default, allow_unmapped
        FROM {}.column_rule
        WHERE mapping_set_id = %s
        ORDER BY rule_id;
        """.format(schema),
        (mapping_set_id,),
    )
    keys = (
        "rule_id", "target_column", "rule_type", "source_column",
        "constant_value", "expression_template", "lookup_schema",
        "lookup_table", "lookup_match_column", "lookup_return_column",
        "unmapped_default", "allow_unmapped",
    )
    rules = [dict(zip(keys, row)) for row in cursor.fetchall()]

    for rule in rules:
        rule["value_map"] = {}
        if rule["rule_type"] == "VALUE_MAP":
            cursor.execute(
                """
                SELECT source_value, target_value
                FROM {}.value_map
                WHERE rule_id = %s;
                """.format(schema),
                (rule["rule_id"],),
            )
            rule["value_map"] = {src: tgt for src, tgt in cursor.fetchall()}

    return rules


def get_discarded_columns(cursor, meta_schema: str, mapping_set_id: int) -> List[str]:
    cursor.execute(
        """
        SELECT source_column
        FROM {}.discarded_source_column
        WHERE mapping_set_id = %s
        ORDER BY source_column;
        """.format(quote_ident(meta_schema)),
        (mapping_set_id,),
    )
    return [row[0] for row in cursor.fetchall()]


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def upsert_mapping_set(
    cursor, meta_schema: str, mapping_name: str,
    source_schema: str, source_table: str,
    target_schema: str, target_table: str,
    row_filter: Optional[str],
) -> int:
    """Create or replace a mapping set, clearing any prior rules for it."""
    schema = quote_ident(meta_schema)
    cursor.execute(
        """
        INSERT INTO {schema}.mapping_set
            (mapping_name, source_schema, source_table,
             target_schema, target_table, row_filter)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (mapping_name) DO UPDATE SET
            source_schema = EXCLUDED.source_schema,
            source_table  = EXCLUDED.source_table,
            target_schema = EXCLUDED.target_schema,
            target_table  = EXCLUDED.target_table,
            row_filter    = EXCLUDED.row_filter
        RETURNING mapping_set_id;
        """.format(schema=schema),
        (mapping_name, source_schema, source_table,
         target_schema, target_table, row_filter),
    )
    mapping_set_id = cursor.fetchone()[0]

    cursor.execute(
        "DELETE FROM {}.column_rule WHERE mapping_set_id = %s;".format(schema),
        (mapping_set_id,),
    )
    cursor.execute(
        "DELETE FROM {}.discarded_source_column WHERE mapping_set_id = %s;".format(schema),
        (mapping_set_id,),
    )
    return mapping_set_id


def insert_column_rule(cursor, meta_schema: str, mapping_set_id: int,
                       rule: Dict) -> int:
    cursor.execute(
        """
        INSERT INTO {}.column_rule
            (mapping_set_id, target_column, rule_type, source_column,
             constant_value, expression_template, lookup_schema, lookup_table,
             lookup_match_column, lookup_return_column,
             unmapped_default, allow_unmapped)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING rule_id;
        """.format(quote_ident(meta_schema)),
        (
            mapping_set_id,
            rule["target_column"],
            rule["rule_type"],
            rule.get("source_column"),
            rule.get("constant_value"),
            rule.get("expression_template"),
            rule.get("lookup_schema"),
            rule.get("lookup_table"),
            rule.get("lookup_match_column"),
            rule.get("lookup_return_column"),
            rule.get("unmapped_default"),
            bool(rule.get("allow_unmapped", False)),
        ),
    )
    return cursor.fetchone()[0]


def insert_value_map(cursor, meta_schema: str, rule_id: int,
                     pairs: Dict[str, str]) -> None:
    for source_value, target_value in pairs.items():
        cursor.execute(
            """
            INSERT INTO {}.value_map (rule_id, source_value, target_value)
            VALUES (%s, %s, %s)
            ON CONFLICT (rule_id, source_value) DO UPDATE
                SET target_value = EXCLUDED.target_value;
            """.format(quote_ident(meta_schema)),
            (rule_id, source_value, target_value),
        )


def derive_discarded_columns(rules: List[Dict], stage_columns: List[str]) -> List[str]:
    """Return staging columns no rule consumes.

    Shared by the mapping wizard and the verification harness so that a
    discard is derived the same way whichever path built the mapping. A column
    counts as consumed if a rule reads it directly or an expression template
    references it.
    """
    consumed = set()
    for rule in rules:
        source_column = rule.get("source_column")
        if source_column:
            consumed.add(source_column)
        template = rule.get("expression_template") or ""
        for column in stage_columns:
            if "{" + column + "}" in template:
                consumed.add(column)
    return [column for column in stage_columns if column not in consumed]


def insert_discarded_columns(cursor, meta_schema: str, mapping_set_id: int,
                             columns: List[str]) -> None:
    for column in columns:
        cursor.execute(
            """
            INSERT INTO {}.discarded_source_column (mapping_set_id, source_column)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING;
            """.format(quote_ident(meta_schema)),
            (mapping_set_id, column),
        )


def log_ingestion(cursor, meta_schema: str, **kwargs) -> None:
    cursor.execute(
        """
        INSERT INTO {}.ingestion_log
            (source_file, loaded_file, was_converted,
             stage_schema, stage_table, column_count, row_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
        """.format(quote_ident(meta_schema)),
        (
            kwargs["source_file"], kwargs["loaded_file"], kwargs["was_converted"],
            kwargs["stage_schema"], kwargs["stage_table"],
            kwargs["column_count"], kwargs["row_count"],
        ),
    )


def log_run(cursor, meta_schema: str, **kwargs) -> None:
    cursor.execute(
        """
        INSERT INTO {}.etl_run
            (mapping_name, source_rows, inserted_rows, failed_rows,
             status, started_at, message)
        VALUES (%s, %s, %s, %s, %s, %s, %s);
        """.format(quote_ident(meta_schema)),
        (
            kwargs["mapping_name"], kwargs["source_rows"], kwargs["inserted_rows"],
            kwargs["failed_rows"], kwargs["status"], kwargs["started_at"],
            kwargs.get("message"),
        ),
    )
