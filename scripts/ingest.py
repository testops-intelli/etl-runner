"""Ingestion wizard: source file to staging table.

Walks the user through:
  1. choosing a file from the source directory
  2. converting .xlsx to .csv if required (same file stem retained)
  3. naming the staging table, defaulting to the file name
  4. creating the staging table from the file's own header row
  5. loading every data row

Staging columns are created as TEXT deliberately. A raw client extract should
land losslessly; a malformed date should not abort ingestion. Type conversion
is a mapping decision, applied later by the ETL runner, where a failure can be
attributed to a specific row and column.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2

from etl.config import load_config
from etl.ingestion import ingest_file, resolve_to_csv
from etl.naming import normalize_headers, normalize_identifier
from etl.source_files import (
    count_csv_data_rows,
    list_source_files,
    read_csv_header,
    sheet_names,
)


def prompt(message: str, default: str = None) -> str:
    suffix = " [{}]".format(default) if default else ""
    while True:
        answer = input("{}{}: ".format(message, suffix)).strip()
        if answer:
            return answer
        if default is not None:
            return default


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
        print("  Please answer y or n.")


def choose_file(config) -> Path:
    files = list_source_files(config.source_dir)
    if not files:
        sys.exit(
            "No .csv or .xlsx files found in {}\n"
            "Place the client extract there and run this again.".format(
                config.source_dir
            )
        )

    print("Files available in {}:".format(config.source_dir))
    for index, path in enumerate(files, start=1):
        print("  {}. {}".format(index, path.name))
    print()

    while True:
        answer = prompt("Select a file by number or name").strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(files):
                return files[index - 1]
            print("  No file numbered {}.".format(index))
            continue

        candidates = [p for p in files if p.name.lower() == answer.lower()]
        if candidates:
            return candidates[0]
        candidates = [p for p in files if p.stem.lower() == answer.lower()]
        if candidates:
            return candidates[0]
        print("  '{}' is not in the list.".format(answer))


def resolve_csv(source_path: Path):
    """Return (csv_path, was_converted). Converts .xlsx when required."""
    if source_path.suffix.lower() == ".csv":
        return source_path, False

    names = sheet_names(source_path)
    chosen = None
    if len(names) > 1:
        print()
        print("'{}' contains {} sheets:".format(source_path.name, len(names)))
        for index, name in enumerate(names, start=1):
            print("  {}. {}".format(index, name))
        answer = prompt("Select a sheet by number", "1")
        chosen = names[int(answer) - 1] if answer.isdigit() else answer

    csv_path, _, row_count = resolve_to_csv(source_path, chosen)
    print()
    print("Converted '{}' to '{}' ({} data rows).".format(
        source_path.name, csv_path.name, row_count))
    print("The database loads CSV only; the file name is unchanged.")
    return csv_path, True


def choose_stage_table(config, csv_path: Path) -> str:
    default_name = normalize_identifier(csv_path.stem)
    print()
    print("Default staging table name from the file: {}.{}".format(
        config.stage_schema, default_name))

    if prompt_yes_no("Use the file name for the staging table?", default=True):
        return default_name

    while True:
        raw = prompt("Enter the staging table name")
        cleaned = normalize_identifier(raw)
        if cleaned != raw:
            print("  Adjusted to a valid SQL identifier: '{}'".format(cleaned))
        if prompt_yes_no("Use '{}.{}'?".format(config.stage_schema, cleaned),
                         default=True):
            return cleaned


def main() -> None:
    config = load_config()

    print("=" * 68)
    print("INGESTION WIZARD - source file to staging table")
    print("=" * 68)
    print()

    source_path = choose_file(config)
    csv_path, was_converted = resolve_csv(source_path)

    raw_headers = read_csv_header(csv_path)
    columns, notes = normalize_headers(raw_headers)

    print()
    print("Header row: {} columns detected.".format(len(columns)))
    if notes:
        print("Column names adjusted for SQL safety:")
        for note in notes:
            print("  - {}".format(note))

    stage_table = choose_stage_table(config, csv_path)

    expected_rows = count_csv_data_rows(csv_path)
    print()
    print("Ready to load {} data rows into {}.{}".format(
        expected_rows, config.stage_schema, stage_table))
    print("All staging columns are created as TEXT so ingestion is lossless.")
    if not prompt_yes_no("Proceed?", default=True):
        sys.exit("Cancelled. Nothing was written.")

    connection = config.connect()
    try:
        with connection:
            result = ingest_file(connection, config, source_path, stage_table)
    finally:
        connection.close()

    loaded = result["loaded_rows"]

    print()
    print("-" * 68)
    if loaded == expected_rows:
        print("INGESTION COMPLETE: {}/{} rows loaded into {}.{}".format(
            loaded, expected_rows, config.stage_schema, stage_table))
    else:
        print("INGESTION WARNING: {} rows loaded, {} expected from the file.".format(
            loaded, expected_rows))
    print("-" * 68)
    print()
    print("Next: python scripts/map_wizard.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except psycopg2.Error as exc:
        sys.exit("DATABASE ERROR: {}".format(str(exc).strip()))
