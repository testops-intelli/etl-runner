"""Create the framework environment.

Creates, in order:
  1. the framework database
  2. the stage, prod and metadata schemas
  3. the production target tables (the fixed contract the ETL maps into)
  4. the metadata registry tables

Production tables are created here, not by the ETL, because production is a
pre-existing contract. Staging is the opposite: it is created at ingestion
time from whatever the client actually sent.

Safe to re-run. Existing objects are left alone unless --reset is passed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

from etl.config import load_config
from etl.naming import quote_ident
from etl.registry import create_registry

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
PROD_DDL_FILE = SQL_DIR / "prod_tables.sql"
REF_DDL_FILE = SQL_DIR / "reference_data.sql"


def create_database(config) -> bool:
    """Create the framework database if absent. Returns True if created."""
    connection = config.connect_admin()
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s;", (config.etl_db,)
            )
            if cursor.fetchone():
                return False
            cursor.execute(
                "CREATE DATABASE {};".format(quote_ident(config.etl_db))
            )
            return True
    finally:
        connection.close()


def drop_database(config) -> None:
    connection = config.connect_admin()
    connection.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid();
                """,
                (config.etl_db,),
            )
            cursor.execute(
                "DROP DATABASE IF EXISTS {};".format(quote_ident(config.etl_db))
            )
    finally:
        connection.close()


def build_schemas_and_objects(config) -> None:
    connection = config.connect()
    try:
        with connection:
            with connection.cursor() as cursor:
                for schema in (config.stage_schema, config.prod_schema,
                               config.ref_schema):
                    cursor.execute(
                        "CREATE SCHEMA IF NOT EXISTS {};".format(quote_ident(schema))
                    )
                    print("  schema ready: {}".format(schema))

                create_registry(cursor, config.meta_schema)
                print("  schema ready: {} (metadata registry)".format(
                    config.meta_schema))

                for ddl_file, token, value, label in (
                    (REF_DDL_FILE, "${REF_SCHEMA}", config.ref_schema,
                     "reference data"),
                    (PROD_DDL_FILE, "${PROD_SCHEMA}", config.prod_schema,
                     "production tables"),
                ):
                    if not ddl_file.exists():
                        sys.exit(
                            "ERROR: DDL file not found at {}".format(ddl_file)
                        )
                    ddl = ddl_file.read_text(encoding="utf-8")
                    ddl = ddl.replace(token, value)
                    cursor.execute(ddl)
                    print("  {} created from sql/{}".format(label, ddl_file.name))

                cursor.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY table_name;
                    """,
                    (config.ref_schema,),
                )
                for (name,) in cursor.fetchall():
                    cursor.execute(
                        "SELECT COUNT(*) FROM {}.{};".format(
                            quote_ident(config.ref_schema), quote_ident(name))
                    )
                    print("    - {}.{} ({} rows)".format(
                        config.ref_schema, name, cursor.fetchone()[0]))

                cursor.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY table_name;
                    """,
                    (config.prod_schema,),
                )
                for (name,) in cursor.fetchall():
                    print("    - {}.{}".format(config.prod_schema, name))
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create the ETL migration framework environment."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop the framework database first, destroying all data in it",
    )
    args = parser.parse_args()

    config = load_config()

    print("Target server : {}:{}".format(config.host, config.port))
    print("Database      : {}".format(config.etl_db))
    print()

    if args.reset:
        answer = input(
            "This will DROP the database '{}' and everything in it. "
            "Type the database name to confirm: ".format(config.etl_db)
        ).strip()
        if answer != config.etl_db:
            sys.exit("Aborted: confirmation did not match.")
        drop_database(config)
        print("Dropped database {}.".format(config.etl_db))

    created = create_database(config)
    print("Database {}: {}".format(
        config.etl_db, "created" if created else "already present"))

    build_schemas_and_objects(config)

    print()
    print("Environment ready.")
    print("Next: python scripts/ingest.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    except psycopg2.Error as exc:
        sys.exit("DATABASE ERROR: {}".format(str(exc).strip()))
