"""Non-interactive ingestion core.

The wizard in scripts/ingest.py collects the decisions; this module performs
them. Keeping the mechanics here means the verification harness exercises the
same load path the wizard uses rather than a reimplementation of it.
"""

from pathlib import Path
from typing import Dict, List

from .naming import normalize_headers, quote_ident
from .registry import log_ingestion
from .source_files import (
    convert_xlsx_to_csv,
    count_csv_data_rows,
    read_csv_header,
)


def resolve_to_csv(source_path: Path, sheet_name: str = None):
    """Return (csv_path, was_converted, converted_row_count)."""
    if source_path.suffix.lower() == ".csv":
        return source_path, False, None
    csv_path, row_count = convert_xlsx_to_csv(source_path, sheet_name)
    return csv_path, True, row_count


def create_stage_table(cursor, schema: str, table: str,
                       columns: List[str]) -> None:
    """Create the staging table, replacing any existing one.

    Every column is TEXT so a raw extract lands losslessly. Type conversion is
    a mapping decision applied later by the runner, where a failure can be
    attributed to a specific row and column.
    """
    column_ddl = ",\n    ".join(
        "{} TEXT".format(quote_ident(name)) for name in columns
    )
    cursor.execute(
        "DROP TABLE IF EXISTS {}.{};".format(
            quote_ident(schema), quote_ident(table))
    )
    cursor.execute(
        "CREATE TABLE {}.{} (\n    {}\n);".format(
            quote_ident(schema), quote_ident(table), column_ddl
        )
    )


def load_csv(cursor, schema: str, table: str, columns: List[str],
             csv_path: Path) -> int:
    """COPY the CSV into the staging table and return the resulting row count."""
    column_list = ", ".join(quote_ident(name) for name in columns)
    copy_sql = (
        "COPY {}.{} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)".format(
            quote_ident(schema), quote_ident(table), column_list
        )
    )
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        cursor.copy_expert(copy_sql, handle)

    cursor.execute(
        "SELECT COUNT(*) FROM {}.{};".format(
            quote_ident(schema), quote_ident(table))
    )
    return cursor.fetchone()[0]


def ingest_file(connection, config, source_path: Path, stage_table: str,
                sheet_name: str = None) -> Dict:
    """Convert if needed, create the staging table, load it, log it.

    Runs inside the caller's transaction. Returns a summary dict.
    """
    csv_path, was_converted, _ = resolve_to_csv(source_path, sheet_name)
    raw_headers = read_csv_header(csv_path)
    columns, notes = normalize_headers(raw_headers)
    expected_rows = count_csv_data_rows(csv_path)

    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE SCHEMA IF NOT EXISTS {};".format(
                quote_ident(config.stage_schema))
        )
        create_stage_table(cursor, config.stage_schema, stage_table, columns)
        loaded = load_csv(
            cursor, config.stage_schema, stage_table, columns, csv_path)
        log_ingestion(
            cursor,
            config.meta_schema,
            source_file=source_path.name,
            loaded_file=csv_path.name,
            was_converted=was_converted,
            stage_schema=config.stage_schema,
            stage_table=stage_table,
            column_count=len(columns),
            row_count=loaded,
        )

    return {
        "csv_path": csv_path,
        "was_converted": was_converted,
        "columns": columns,
        "notes": notes,
        "expected_rows": expected_rows,
        "loaded_rows": loaded,
    }
