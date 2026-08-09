"""Verification harness.

Run it with no setup beyond a populated .env:

    python verify_engine.py

It builds its own disposable database, ingests the extracts in source_files/,
registers mappings, exercises every rule type and every failure path, reports
what it proved, and drops the database again. It never touches the database
named in ETL_DB.

It exists because the interactive wizards cannot easily be driven in a scripted
run, and because the behaviour worth trusting in this framework is the failure
behaviour, which a successful run never demonstrates.
"""

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Redirect this run onto a scratch database before the config is read.
# load_dotenv does not override variables already set in the environment, so
# this wins over the ETL_DB value in .env.
ENV_FILE = PROJECT_ROOT / ".env"
if not ENV_FILE.exists():
    sys.exit(
        "ERROR: no .env file found at {}\n"
        "Copy .env.example to .env and fill in your values.".format(ENV_FILE)
    )

BASE_DB = None
for line in ENV_FILE.read_text().splitlines():
    if line.strip().startswith("ETL_DB="):
        BASE_DB = line.split("=", 1)[1].strip()
VERIFY_DB = "{}_verify".format(BASE_DB or "etl_migration")
os.environ["ETL_DB"] = VERIFY_DB

import psycopg2  # noqa: E402
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402

from etl.config import load_config  # noqa: E402
from etl.ingestion import ingest_file  # noqa: E402
from etl.registry import (  # noqa: E402
    create_registry,
    derive_discarded_columns,
    insert_column_rule,
    insert_discarded_columns,
    insert_value_map,
    upsert_mapping_set,
)
from etl.transform import get_stage_columns  # noqa: E402

config = load_config()
META = config.meta_schema
SQL_DIR = PROJECT_ROOT / "sql"

RESULTS = []


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

def banner(text):
    print()
    print("#" * 70)
    print("# {}".format(text))
    print("#" * 70)


def check(label, condition, detail=""):
    RESULTS.append((label, bool(condition)))
    print("  [{}] {}{}".format(
        "PASS" if condition else "FAIL", label,
        "  -- {}".format(detail) if detail else ""))
    if not condition:
        raise AssertionError(label)


def sql(statements):
    conn = config.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    conn.close()


def query(statement):
    conn = config.connect()
    with conn.cursor() as cur:
        cur.execute(statement)
        rows = cur.fetchall()
    conn.close()
    return rows


def drop_verify_db():
    conn = config.connect_admin()
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid();", (VERIFY_DB,))
        cur.execute('DROP DATABASE IF EXISTS "{}";'.format(VERIFY_DB))
    conn.close()


def build_verify_db():
    drop_verify_db()
    conn = config.connect_admin()
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute('CREATE DATABASE "{}";'.format(VERIFY_DB))
    conn.close()

    conn = config.connect()
    with conn:
        with conn.cursor() as cur:
            for schema in (config.stage_schema, config.prod_schema,
                           config.ref_schema):
                cur.execute('CREATE SCHEMA IF NOT EXISTS "{}";'.format(schema))
            create_registry(cur, META)
            ref_ddl = (SQL_DIR / "reference_data.sql").read_text()
            cur.execute(ref_ddl.replace("${REF_SCHEMA}", config.ref_schema))
            prod_ddl = (SQL_DIR / "prod_tables.sql").read_text()
            cur.execute(prod_ddl.replace("${PROD_SCHEMA}", config.prod_schema))
    conn.close()


def ingest(filename, stage_table):
    source_path = config.source_dir / filename
    if not source_path.exists():
        sys.exit("Missing source file: {}".format(source_path))
    conn = config.connect()
    try:
        with conn:
            result = ingest_file(conn, config, source_path, stage_table)
    finally:
        conn.close()
    return result


def register(name, src_table, tgt_table, rules, row_filter=None):
    conn = config.connect()
    with conn:
        with conn.cursor() as cur:
            mapping_set_id = upsert_mapping_set(
                cur, META, name, config.stage_schema, src_table,
                config.prod_schema, tgt_table, row_filter)
            for rule in rules:
                pairs = rule.get("_value_map")
                payload = {k: v for k, v in rule.items() if k != "_value_map"}
                rule_id = insert_column_rule(cur, META, mapping_set_id, payload)
                if pairs:
                    insert_value_map(cur, META, rule_id, pairs)

            stage_columns = get_stage_columns(
                cur, config.stage_schema, src_table)
            insert_discarded_columns(
                cur, META, mapping_set_id,
                derive_discarded_columns(rules, stage_columns))
    conn.close()


def run_etl(name):
    env = dict(os.environ)
    env["ETL_DB"] = VERIFY_DB
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "etl_runner.py"),
         "--mapping", name],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT),
    )
    return result.returncode, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def main():
    started = dt.datetime.now()
    print("=" * 70)
    print("VERIFICATION HARNESS")
    print("=" * 70)
    print("Scratch database : {}".format(VERIFY_DB))
    print("Source files     : {}".format(config.source_dir))
    print("The database named in ETL_DB ({}) is not touched.".format(BASE_DB))

    build_verify_db()
    print("\nScratch database built: schemas, reference data, prod tables.")

    # -- Ingestion ----------------------------------------------------------
    banner("INGESTION: xlsx conversion, header normalization, load")

    company = ingest("XYZ-Company_Static_Data_2025-12-31.xlsx", "company_data")
    check("company extract loaded 1/1",
          company["loaded_rows"] == company["expected_rows"] == 1)
    check("'Shares Outstanding' normalized to a safe identifier",
          "shares_outstanding" in company["columns"])

    holdings = ingest("XYZ-Shareholder_Holdings_2025-12-31.xlsx", "holdings")
    check("holdings extract loaded 31/31",
          holdings["loaded_rows"] == holdings["expected_rows"] == 31)

    shareholders = ingest(
        "XYZ-Shareholders_Static_Data_2025-12-31.xlsx", "shareholders")
    check("shareholders extract loaded 100/100",
          shareholders["loaded_rows"] == shareholders["expected_rows"] == 100)
    check("duplicate 'Holder_ID' header suffixed, not dropped",
          "holder_id" in shareholders["columns"]
          and "holder_id_2" in shareholders["columns"]
          and len(shareholders["columns"]) == 13)

    corporate = ingest("XYZ-Corporate_Actions-2025-12-31.xlsx",
                       "corporate_actions")
    check("corporate actions extract loaded 3/3",
          corporate["loaded_rows"] == corporate["expected_rows"] == 3)

    transactions = ingest("XYZ-Shareholder_Transactions-2025-12-31.xlsx",
                          "transactions")
    check("transactions extract loaded 128/128",
          transactions["loaded_rows"] == transactions["expected_rows"] == 128)

    # -- Chain step 1: company ---------------------------------------------
    banner("CHAIN STEP 1: company_master  (DIRECT, LOOKUP, VALUE_MAP, NULL)")

    register("company_to_prod", "company_data", "company_master", [
        {"target_column": "company_name", "rule_type": "DIRECT",
         "source_column": "company_name"},
        {"target_column": "ticker", "rule_type": "DIRECT",
         "source_column": "ticker"},
        {"target_column": "exchange_code", "rule_type": "DIRECT",
         "source_column": "exchange"},
        {"target_column": "isin", "rule_type": "DIRECT",
         "source_column": "isin"},
        {"target_column": "sedol", "rule_type": "NULL"},
        {"target_column": "currency_id", "rule_type": "LOOKUP",
         "source_column": "currency", "lookup_schema": config.ref_schema,
         "lookup_table": "currency", "lookup_match_column": "currency_code",
         "lookup_return_column": "currency_id"},
        {"target_column": "shares_outstanding", "rule_type": "DIRECT",
         "source_column": "shares_outstanding"},
        {"target_column": "company_status", "rule_type": "VALUE_MAP",
         "source_column": "status",
         "_value_map": {"ACTIVE": "A", "INACTIVE": "I"}},
        {"target_column": "listing_date", "rule_type": "DIRECT",
         "source_column": "listing_date"},
    ])
    code, out = run_etl("company_to_prod")
    check("company ETL passed 1/1", code == 0)
    row = query("""
        SELECT cm.company_name, cm.company_status, cm.sedol,
               rc.currency_code, cm.shares_outstanding, cm.listing_date
        FROM prod.company_master cm
        LEFT JOIN ref.currency rc ON rc.currency_id = cm.currency_id;
    """)[0]
    check("currency code resolved via ref lookup", row[3] == "AUD", str(row[3]))
    check("status translated Active -> A", row[1] == "A")
    check("sedol deliberately NULL", row[2] is None)
    check("listing_date cast from text to DATE",
          str(row[5]) == "2025-01-01", str(row[5]))
    check("internal_identifier recorded as discarded",
          query("""SELECT COUNT(*) FROM etl_meta.discarded_source_column d
                   JOIN etl_meta.mapping_set m USING (mapping_set_id)
                   WHERE m.mapping_name='company_to_prod'
                   AND d.source_column='internal_identifier';""")[0][0] == 1)

    # -- Chain step 2: shareholders + the FOUNDATION gap --------------------
    banner("CHAIN STEP 2: shareholder_master  (unmapped value must fail)")

    company_id = query("SELECT company_id FROM prod.company_master;")[0][0]

    # A single-company migration, so company_id as a CONSTANT is honest. The
    # point is that it now lives in the registry as configuration rather than
    # as a literal compiled into the transformation.
    incomplete = [
        {"target_column": "company_id", "rule_type": "CONSTANT",
         "constant_value": str(company_id)},
        {"target_column": "shareholder_name", "rule_type": "DIRECT",
         "source_column": "holder_name"},
        {"target_column": "shareholder_type", "rule_type": "VALUE_MAP",
         "source_column": "holder_type",
         "_value_map": {"INDIVIDUAL": "I", "FUND": "F", "TRUST": "T",
                        "JOINT": "J", "SMSF": "S"}},
        {"target_column": "residency_country_id", "rule_type": "LOOKUP",
         "source_column": "residency_country",
         "lookup_schema": config.ref_schema, "lookup_table": "country",
         "lookup_match_column": "country_code",
         "lookup_return_column": "country_id"},
        {"target_column": "currency_id", "rule_type": "LOOKUP",
         "source_column": "currency",
         "lookup_schema": config.ref_schema, "lookup_table": "currency",
         "lookup_match_column": "currency_code",
         "lookup_return_column": "currency_id"},
        {"target_column": "shareholder_status", "rule_type": "VALUE_MAP",
         "source_column": "holder_status",
         "_value_map": {"ACTIVE": "A", "CLOSED": "C", "SUSPENDED": "S",
                        "DECEASED": "D", "DORMANT": "O"}},
        {"target_column": "holder_reference_type", "rule_type": "DIRECT",
         "source_column": "holder_reference_type"},
        {"target_column": "holder_reference_number", "rule_type": "DIRECT",
         "source_column": "holder_reference_number"},
        {"target_column": "communication_preference", "rule_type": "VALUE_MAP",
         "source_column": "communication_preference",
         "_value_map": {"EMAIL": "E", "POST": "P"}},
        # FOUNDATION deliberately omitted. The original repository's ETL mapped
        # FOUNDER, a value the data never contains, so every foundation holder
        # silently fell through to a catch-all and nobody noticed.
        {"target_column": "holder_category", "rule_type": "VALUE_MAP",
         "source_column": "holder_category",
         "_value_map": {"RETAIL": "R", "INSTITUTIONAL": "I"}},
    ]
    register("shareholders_to_prod", "shareholders", "shareholder_master",
             incomplete)
    code, out = run_etl("shareholders_to_prod")
    check("run failed on the unmapped value", code == 1)
    check("failure report names FOUNDATION", "FOUNDATION" in out)
    check("nothing committed on failure",
          query("SELECT COUNT(*) FROM prod.shareholder_master;")[0][0] == 0)

    complete = [dict(rule) for rule in incomplete]
    complete[9]["_value_map"] = {"RETAIL": "R", "INSTITUTIONAL": "I",
                                 "FOUNDATION": "F"}
    register("shareholders_to_prod", "shareholders", "shareholder_master",
             complete)
    code, out = run_etl("shareholders_to_prod")
    check("run passed once every value is mapped", code == 0)
    check("100/100 shareholders committed",
          query("SELECT COUNT(*) FROM prod.shareholder_master;")[0][0] == 100)
    check("company_id carried through to every shareholder",
          query("SELECT COUNT(*) FROM prod.shareholder_master "
                "WHERE company_id IS NULL;")[0][0] == 0)

    # -- Chain step 3: holdings, resolving both keys ------------------------
    banner("CHAIN STEP 3: holdings  (both foreign keys resolved by LOOKUP)")

    holdings_rules = [
        {"target_column": "company_id", "rule_type": "LOOKUP",
         "source_column": "identifier", "lookup_schema": config.prod_schema,
         "lookup_table": "company_master", "lookup_match_column": "isin",
         "lookup_return_column": "company_id"},
        {"target_column": "shareholder_id", "rule_type": "LOOKUP",
         "source_column": "holder", "lookup_schema": config.prod_schema,
         "lookup_table": "shareholder_master",
         "lookup_match_column": "shareholder_name",
         "lookup_return_column": "shareholder_id"},
        {"target_column": "identifier", "rule_type": "DIRECT",
         "source_column": "identifier"},
        {"target_column": "units", "rule_type": "DIRECT",
         "source_column": "units"},
        {"target_column": "as_of_date", "rule_type": "DIRECT",
         "source_column": "as_of_date"},
    ]
    register("holdings_to_prod", "holdings", "holdings", holdings_rules)
    code, out = run_etl("holdings_to_prod")
    check("holdings ETL passed 31/31", code == 0)
    check("every company_id resolved by ISIN",
          query("SELECT COUNT(*) FROM prod.holdings "
                "WHERE company_id IS NULL;")[0][0] == 0)
    check("every shareholder_id resolved by name",
          query("SELECT COUNT(*) FROM prod.holdings "
                "WHERE shareholder_id IS NULL;")[0][0] == 0)
    total = query("SELECT SUM(units) FROM prod.holdings;")[0][0]
    outstanding = query(
        "SELECT shares_outstanding FROM prod.company_master;")[0][0]
    check("migrated units reconcile to shares outstanding",
          total == outstanding, "{} vs {}".format(total, outstanding))

    # -- Failure paths ------------------------------------------------------
    banner("FAILURE PATHS: bad cast, unresolvable lookup, rollback")

    before = query("SELECT COUNT(*) FROM prod.holdings;")[0][0]
    sql(["UPDATE stage.holdings SET units = 'not-a-number' "
         "WHERE holder_id = 'H0001';",
         "UPDATE stage.holdings SET as_of_date = '31/13/2025' "
         "WHERE holder_id = 'H0002';",
         "UPDATE stage.holdings SET holder = 'Ghost Investor Pty Ltd' "
         "WHERE holder_id = 'H0003';"])
    code, out = run_etl("holdings_to_prod")
    after = query("SELECT COUNT(*) FROM prod.holdings;")[0][0]
    check("run failed", code == 1)
    check("bad numeric reported by row and column",
          "not-a-number" in out and "units" in out)
    check("unparseable date reported", "31/13/2025" in out)
    check("unresolvable lookup reported", "Ghost Investor" in out)
    check("all three failures reported in a single pass",
          "Rows failed: 3" in out)
    check("transaction rolled back, production unchanged",
          before == after, "{} rows before and after".format(before))

    sql(["UPDATE stage.holdings SET units = '48750' "
         "WHERE holder_id = 'H0001';",
         "UPDATE stage.holdings SET as_of_date = '2025-12-31' "
         "WHERE holder_id = 'H0002';",
         "UPDATE stage.holdings SET holder = 'James Walker' "
         "WHERE holder_id = 'H0003';"])

    # -- Chain step 4: corporate actions ------------------------------------
    banner("CHAIN STEP 4: corporate_actions  (EXPRESSION, VALUE_MAP)")

    register("corporate_actions_to_prod", "corporate_actions",
             "corporate_actions", [
        {"target_column": "company_id", "rule_type": "CONSTANT",
         "constant_value": str(company_id)},
        {"target_column": "action_code", "rule_type": "EXPRESSION",
         "expression_template": "CA_{action_id}"},
        {"target_column": "action_type", "rule_type": "DIRECT",
         "source_column": "type"},
        {"target_column": "ex_date", "rule_type": "DIRECT",
         "source_column": "ex_date"},
        {"target_column": "record_date", "rule_type": "DIRECT",
         "source_column": "record_date"},
        {"target_column": "payment_date", "rule_type": "DIRECT",
         "source_column": "payment_date"},
        {"target_column": "effective_date", "rule_type": "DIRECT",
         "source_column": "effective_date"},
        {"target_column": "drp_price", "rule_type": "DIRECT",
         "source_column": "drp_price"},
        {"target_column": "amount_per_unit", "rule_type": "DIRECT",
         "source_column": "amount_per_unit"},
        {"target_column": "split_ratio", "rule_type": "DIRECT",
         "source_column": "stock_split_ratio"},
    ])
    code, out = run_etl("corporate_actions_to_prod")
    check("corporate actions ETL passed 3/3", code == 0)
    rows = query("SELECT action_code, action_type, split_ratio, "
                 "amount_per_unit FROM prod.corporate_actions "
                 "ORDER BY action_code;")
    check("expression template applied", rows[0][0] == "CA_CA001",
          str(rows[0][0]))
    check("one dividend and two splits loaded",
          [r[1] for r in rows] == ["DIVIDEND", "SPLIT", "SPLIT"],
          str([r[1] for r in rows]))
    check("fields not applicable to an action type left NULL",
          rows[0][2] is None and rows[1][3] is None,
          "dividend split_ratio={}, split amount_per_unit={}".format(
              rows[0][2], rows[1][3]))

    # -- Chain step 5: transactions -----------------------------------------
    banner("CHAIN STEP 5: share_registry_transactions  (both keys by LOOKUP)")

    register("transactions_to_prod", "transactions",
             "share_registry_transactions", [
        {"target_column": "company_id", "rule_type": "CONSTANT",
         "constant_value": str(company_id)},
        {"target_column": "shareholder_id", "rule_type": "LOOKUP",
         "source_column": "holder", "lookup_schema": config.prod_schema,
         "lookup_table": "shareholder_master",
         "lookup_match_column": "shareholder_name",
         "lookup_return_column": "shareholder_id"},
        {"target_column": "source_transaction_id", "rule_type": "DIRECT",
         "source_column": "txn_id"},
        {"target_column": "transaction_type", "rule_type": "DIRECT",
         "source_column": "type"},
        {"target_column": "units", "rule_type": "DIRECT",
         "source_column": "units"},
        {"target_column": "transaction_date", "rule_type": "DIRECT",
         "source_column": "date"},
        {"target_column": "transaction_description", "rule_type": "DIRECT",
         "source_column": "description"},
    ])
    code, out = run_etl("transactions_to_prod")
    check("transactions ETL passed 128/128", code == 0)
    check("every shareholder_id resolved",
          query("SELECT COUNT(*) FROM prod.share_registry_transactions "
                "WHERE shareholder_id IS NULL;")[0][0] == 0)
    txn_total = query(
        "SELECT SUM(units) FROM prod.share_registry_transactions;")[0][0]
    check("transaction units reconcile to holdings units",
          txn_total == query("SELECT SUM(units) FROM prod.holdings;")[0][0],
          str(txn_total))

    # -- Pre-flight schema validation ---------------------------------------
    banner("PRE-FLIGHT VALIDATION: mapping checked against the live schema")

    register("ca_broken", "corporate_actions", "corporate_actions", [
        {"target_column": "company_id", "rule_type": "CONSTANT",
         "constant_value": str(company_id)},
        {"target_column": "action_code", "rule_type": "DIRECT",
         "source_column": "column_that_does_not_exist"},
        {"target_column": "ex_date", "rule_type": "DIRECT",
         "source_column": "ex_date"},
    ])
    ca_before = query("SELECT COUNT(*) FROM prod.corporate_actions;")[0][0]
    code, out = run_etl("ca_broken")
    check("run rejected before processing any row", code == 2)
    check("report names the missing source column",
          "column_that_does_not_exist" in out)
    check("no rows processed by the rejected mapping",
          query("SELECT COUNT(*) FROM prod.corporate_actions;")[0][0]
          == ca_before)

    # -- Registry state -----------------------------------------------------
    banner("REGISTRY: every run recorded, including the failed ones")

    runs = query("SELECT mapping_name, status, source_rows, inserted_rows, "
                 "failed_rows FROM etl_meta.etl_run ORDER BY run_id;")
    for run in runs:
        print("  {:<22} {:<5} source={:<4} inserted={:<4} failed={}".format(*run))
    check("failed runs recorded with zero rows inserted",
          all(r[3] == 0 for r in runs if r[1] == "FAIL"))
    check("passing runs recorded with every row inserted",
          all(r[2] == r[3] for r in runs if r[1] == "PASS"))

    # -- Summary ------------------------------------------------------------
    passed = sum(1 for _, ok in RESULTS if ok)
    banner("ALL {} CHECKS PASSED  ({:.1f}s)".format(
        passed, (dt.datetime.now() - started).total_seconds()))


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except AssertionError as exc:
        print("\nVERIFICATION FAILED: {}".format(exc))
        exit_code = 1
    except psycopg2.Error as exc:
        print("\nDATABASE ERROR: {}".format(str(exc).strip()))
        exit_code = 2
    finally:
        try:
            drop_verify_db()
            print("\nScratch database {} dropped.".format(VERIFY_DB))
        except psycopg2.Error as exc:
            print("\nCould not drop {}: {}".format(VERIFY_DB, exc))
    sys.exit(exit_code)
