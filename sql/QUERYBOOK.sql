-- ===========================================================================
-- QUERY BOOK
--
-- Ad hoc queries for inspecting the framework's state. Nothing here is run by
-- the framework itself; these are for looking at what happened after an
-- ingestion or an ETL run.
--
-- Run against the database named in ETL_DB.
--   psql -h localhost -U <user> -d etl_migration -f sql/QUERYBOOK.sql
-- or paste individual blocks into psql / DBeaver / pgAdmin.
--
-- Schema roles:
--   stage     staging tables, created at ingestion, every column TEXT
--   prod      production targets, the fixed contract
--   ref       reference/lookup data, seeded as a prerequisite
--   etl_meta  the metadata registry: mappings, rules, logs
-- ===========================================================================


-- ---------------------------------------------------------------------------
-- 1. ORIENTATION: what exists right now
-- ---------------------------------------------------------------------------

-- Every table the framework knows about, with its live row count.
SELECT n.nspname                AS schema_name,
       c.relname                AS table_name,
       COALESCE(s.n_live_tup, 0) AS approx_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind = 'r'
  AND n.nspname IN ('stage', 'prod', 'ref', 'etl_meta')
ORDER BY n.nspname, c.relname;


-- Column definitions for one table. Change the two values as needed.
-- Useful before building a mapping: this is what the wizard is reading.
SELECT ordinal_position, column_name, data_type,
       is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'prod'
  AND table_name   = 'company_master'
ORDER BY ordinal_position;


-- ---------------------------------------------------------------------------
-- 2. STAGING: what actually arrived from the source file
-- ---------------------------------------------------------------------------

-- Staging holds raw text exactly as the extract supplied it, so this is the
-- place to look when a cast fails during an ETL run.
SELECT * FROM stage.company_static_data;


-- Ingestion history: every file loaded, whether it was converted from xlsx,
-- and how many rows and columns landed.
SELECT ingestion_id, source_file, loaded_file, was_converted,
       stage_schema || '.' || stage_table AS stage_target,
       column_count, row_count, loaded_at
FROM etl_meta.ingestion_log
ORDER BY ingestion_id DESC;


-- Distinct values in a staging column. Run this before choosing VALUE_MAP so
-- the map is built against the data rather than an assumption about it.
SELECT status AS source_value, COUNT(*) AS row_count
FROM stage.company_static_data
GROUP BY status
ORDER BY row_count DESC;


-- ---------------------------------------------------------------------------
-- 3. PRODUCTION: what the ETL produced
-- ---------------------------------------------------------------------------

-- company_master with its reference lookup resolved back to a readable code.
-- currency_id is a surrogate key; joining ref.currency proves it resolved to
-- the right thing rather than to some arbitrary integer.
SELECT cm.company_id,
       cm.company_name,
       cm.ticker,
       cm.exchange_code,
       cm.isin,
       cm.sedol,
       cm.currency_id,
       rc.currency_code,
       cm.shares_outstanding,
       cm.company_status,
       cm.listing_date
FROM prod.company_master cm
LEFT JOIN ref.currency rc ON rc.currency_id = cm.currency_id;


-- holdings, with both foreign keys resolved back to human-readable values.
SELECT h.holding_id,
       cm.company_name,
       h.identifier,
       h.units,
       h.as_of_date
FROM prod.holdings h
LEFT JOIN prod.company_master cm ON cm.company_id = h.company_id
ORDER BY h.units DESC;


-- shareholder_master with both reference lookups resolved back to codes.
SELECT sm.shareholder_id, sm.shareholder_name, sm.shareholder_type,
       rc2.country_code AS residency, rc.currency_code,
       sm.shareholder_status, sm.holder_reference_type,
       sm.communication_preference, sm.holder_category
FROM prod.shareholder_master sm
LEFT JOIN ref.currency rc  ON rc.currency_id  = sm.currency_id
LEFT JOIN ref.country  rc2 ON rc2.country_id  = sm.residency_country_id
ORDER BY sm.shareholder_id
LIMIT 20;


-- corporate_actions. Fields not applicable to an action type are NULL:
-- a DIVIDEND row carries no split_ratio, a SPLIT row no amount_per_unit.
SELECT corporate_action_id, action_code, action_type, ex_date, record_date,
       payment_date, effective_date, drp_price, amount_per_unit, split_ratio
FROM prod.corporate_actions
ORDER BY action_code;


-- share_registry_transactions with the shareholder name resolved back.
SELECT t.transaction_id, t.source_transaction_id, sm.shareholder_name,
       t.transaction_type, t.units, t.transaction_date,
       t.transaction_description
FROM prod.share_registry_transactions t
LEFT JOIN prod.shareholder_master sm ON sm.shareholder_id = t.shareholder_id
ORDER BY t.transaction_date
LIMIT 20;


-- Transaction totals by type.
SELECT transaction_type, COUNT(*) AS txn_count, SUM(units) AS total_units
FROM prod.share_registry_transactions
GROUP BY transaction_type
ORDER BY total_units DESC;


-- Reference data available to LOOKUP rules.
SELECT currency_id, currency_code, currency_name FROM ref.currency
ORDER BY currency_id;

SELECT country_id, country_code, country_name FROM ref.country
ORDER BY country_id;


-- ---------------------------------------------------------------------------
-- 4. THE REGISTRY: how the transformation was configured
--
-- This is the part worth showing someone. No table name, column name,
-- constant or code translation lives in the Python or SQL source; it is all
-- rows here, which is what lets one runner serve any table pair.
-- ---------------------------------------------------------------------------

-- Every registered mapping.
SELECT mapping_set_id,
       mapping_name,
       source_schema || '.' || source_table AS source,
       target_schema || '.' || target_table AS target,
       COALESCE(row_filter, '(all rows)')   AS row_filter,
       created_at
FROM etl_meta.mapping_set
ORDER BY mapping_set_id;


-- The full rule set for one mapping, rendered readably.
-- Change the mapping_name to inspect a different one.
SELECT cr.target_column,
       cr.rule_type,
       CASE cr.rule_type
           WHEN 'DIRECT'     THEN 'from ' || cr.source_column
           WHEN 'CONSTANT'   THEN '= ' || cr.constant_value
           WHEN 'NULL'       THEN '(deliberately empty)'
           WHEN 'EXPRESSION' THEN '= ' || cr.expression_template
           WHEN 'VALUE_MAP'  THEN 'from ' || cr.source_column
                                   || ' via value map'
           WHEN 'LOOKUP'     THEN 'from ' || cr.source_column || ' via '
                                   || cr.lookup_schema || '.'
                                   || cr.lookup_table || '.'
                                   || cr.lookup_match_column
                                   || ' -> ' || cr.lookup_return_column
       END AS detail,
       CASE WHEN cr.allow_unmapped
            THEN 'fallback: ' || COALESCE(cr.unmapped_default, 'NULL')
            ELSE 'no fallback (unmapped values fail the row)'
       END AS unmapped_behaviour
FROM etl_meta.column_rule cr
JOIN etl_meta.mapping_set ms USING (mapping_set_id)
WHERE ms.mapping_name = 'company_to_prod'
ORDER BY cr.rule_id;


-- Every value translation recorded, across all mappings.
SELECT ms.mapping_name,
       cr.target_column,
       cr.source_column,
       vm.source_value,
       vm.target_value
FROM etl_meta.value_map vm
JOIN etl_meta.column_rule cr USING (rule_id)
JOIN etl_meta.mapping_set ms USING (mapping_set_id)
ORDER BY ms.mapping_name, cr.target_column, vm.source_value;


-- Source columns deliberately not carried across. A discard is recorded as a
-- decision rather than left as an omission nobody noticed.
SELECT ms.mapping_name, d.source_column
FROM etl_meta.discarded_source_column d
JOIN etl_meta.mapping_set ms USING (mapping_set_id)
ORDER BY ms.mapping_name, d.source_column;


-- ---------------------------------------------------------------------------
-- 5. RUN HISTORY
-- ---------------------------------------------------------------------------

-- Every run, including the ones that failed and rolled back. A FAIL row always
-- shows inserted_rows = 0, because a failed run commits nothing.
SELECT run_id,
       mapping_name,
       status,
       source_rows,
       inserted_rows,
       failed_rows,
       message,
       finished_at
FROM etl_meta.etl_run
ORDER BY run_id DESC;


-- Latest outcome per mapping.
SELECT DISTINCT ON (mapping_name)
       mapping_name, status, source_rows, inserted_rows,
       failed_rows, finished_at
FROM etl_meta.etl_run
ORDER BY mapping_name, run_id DESC;


-- ---------------------------------------------------------------------------
-- 6. RECONCILIATION SPOT CHECKS
--
-- These are completeness and consistency checks only. Full semantic
-- validation is a separate concern and a separate script.
-- ---------------------------------------------------------------------------

-- Staging row count vs production row count for one mapping. These should
-- agree for a mapping with no row filter.
SELECT (SELECT COUNT(*) FROM stage.company_static_data) AS staging_rows,
       (SELECT COUNT(*) FROM prod.company_master)       AS production_rows;


-- Unresolved foreign keys. Anything other than zero means a LOOKUP wrote NULL
-- rather than resolving, which only happens when the source value was empty.
SELECT COUNT(*) FILTER (WHERE company_id IS NULL)     AS null_company_id,
       COUNT(*) FILTER (WHERE shareholder_id IS NULL) AS null_shareholder_id,
       COUNT(*)                                       AS total_rows
FROM prod.holdings;


-- Do the transaction ledger and the holdings snapshot agree?
SELECT (SELECT SUM(units) FROM prod.share_registry_transactions)
           AS transaction_units,
       (SELECT SUM(units) FROM prod.holdings) AS holdings_units,
       (SELECT SUM(units) FROM prod.share_registry_transactions)
           - (SELECT SUM(units) FROM prod.holdings) AS variance;


-- Row counts across the whole chain, in load order.
SELECT 'company_master'              AS target_table,
       COUNT(*) FROM prod.company_master
UNION ALL SELECT 'shareholder_master',
       COUNT(*) FROM prod.shareholder_master
UNION ALL SELECT 'corporate_actions',
       COUNT(*) FROM prod.corporate_actions
UNION ALL SELECT 'share_registry_transactions',
       COUNT(*) FROM prod.share_registry_transactions
UNION ALL SELECT 'holdings',
       COUNT(*) FROM prod.holdings;


-- Do the migrated holdings reconcile to the company's shares outstanding?
SELECT cm.company_name,
       cm.shares_outstanding,
       SUM(h.units)                          AS migrated_units,
       SUM(h.units) - cm.shares_outstanding  AS variance
FROM prod.company_master cm
LEFT JOIN prod.holdings h ON h.company_id = cm.company_id
GROUP BY cm.company_name, cm.shares_outstanding;
