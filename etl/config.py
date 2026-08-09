"""Configuration loading and database connection helpers.

All credentials come from a gitignored .env file. See .env.example.
"""

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """Resolved framework configuration."""

    def __init__(self) -> None:
        env_path = PROJECT_ROOT / ".env"
        if not env_path.exists():
            sys.exit(
                "ERROR: no .env file found at {}\n"
                "Copy .env.example to .env and fill in your values.".format(env_path)
            )
        load_dotenv(env_path)

        self.host = self._require("PGHOST")
        self.port = self._require("PGPORT")
        self.user = self._require("PGUSER")
        self.password = os.getenv("PGPASSWORD", "")
        self.admin_db = os.getenv("PGADMIN_DB", "postgres")

        self.etl_db = self._require("ETL_DB")
        self.stage_schema = os.getenv("STAGE_SCHEMA", "stage")
        self.prod_schema = os.getenv("PROD_SCHEMA", "prod")
        self.meta_schema = os.getenv("META_SCHEMA", "etl_meta")
        self.ref_schema = os.getenv("REF_SCHEMA", "ref")

        self.source_dir = PROJECT_ROOT / os.getenv("SOURCE_DIR", "source_files")

    @staticmethod
    def _require(key: str) -> str:
        value = os.getenv(key)
        if not value:
            sys.exit(
                "ERROR: required setting {} is missing from .env "
                "(see .env.example).".format(key)
            )
        return value

    def connect_admin(self):
        """Connect to the administrative database (used to CREATE DATABASE)."""
        return self._connect(self.admin_db)

    def connect(self):
        """Connect to the framework database."""
        return self._connect(self.etl_db)

    def _connect(self, dbname: str):
        try:
            return psycopg2.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                dbname=dbname,
            )
        except psycopg2.OperationalError as exc:
            sys.exit(
                "ERROR: could not connect to database '{}' on {}:{} as user '{}'.\n"
                "Check the values in your .env file and that PostgreSQL is running.\n"
                "Driver reported: {}".format(
                    dbname, self.host, self.port, self.user, str(exc).strip()
                )
            )


def load_config() -> Config:
    return Config()
