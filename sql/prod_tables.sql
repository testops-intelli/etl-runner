-- Production target tables.
--
-- These are the fixed contract the ETL maps INTO. They are created by
-- scripts/create_env.py, never by the ingestion or the ETL runner, because
-- production structures exist independently of whatever a client happens to
-- send. Staging is the mirror image: created at ingestion time from the file.
--
-- ${PROD_SCHEMA} is substituted from the PROD_SCHEMA setting in .env.
--
-- ---------------------------------------------------------------------------
-- LOAD ORDER
--
-- These tables form a chain. Each one issues or consumes surrogate keys, so
-- they must be loaded in this order:
--
--   1. company_master              issues company_id
--   2. shareholder_master          consumes company_id, issues shareholder_id
--   3. corporate_actions           consumes company_id
--   4. share_registry_transactions consumes company_id and shareholder_id
--   5. holdings                    consumes company_id and shareholder_id
--
-- The runner does not model this dependency. It fails loudly on an unresolved
-- LOOKUP rather than writing a NULL, so the order is the operator's to follow.
-- Modelling it is listed under extension points in the README.
--
-- One staging table maps to exactly one target table throughout.
-- ---------------------------------------------------------------------------


-- ---------------------------------------------------------------------------
-- 1. company_master          <- XYZ-Company_Static_Data
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${PROD_SCHEMA}.company_master (
    company_id             BIGSERIAL PRIMARY KEY,
    company_name           TEXT,
    ticker                 VARCHAR(20),
    exchange_code          VARCHAR(20),
    isin                   VARCHAR(20),
    sedol                  VARCHAR(20),
    currency_id            INT,
    shares_outstanding     NUMERIC(20,0),
    company_status         VARCHAR(1),
    listing_date           DATE
);


-- ---------------------------------------------------------------------------
-- 2. shareholder_master     <- XYZ-Shareholders_Static_Data
--
-- residency_country_id and currency_id resolve against the ref schema.
-- company_id resolves against company_master.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${PROD_SCHEMA}.shareholder_master (
    shareholder_id             BIGSERIAL PRIMARY KEY,
    company_id                 BIGINT,
    shareholder_name           TEXT,
    shareholder_type           VARCHAR(1),
    residency_country_id       INT,
    currency_id                INT,
    shareholder_status         VARCHAR(1),
    holder_reference_type      VARCHAR(10),
    holder_reference_number    VARCHAR(50),
    communication_preference   VARCHAR(1),
    holder_category            VARCHAR(1)
);


-- ---------------------------------------------------------------------------
-- 3. corporate_actions      <- XYZ-Corporate_Actions
--
-- NOTE ON PROVENANCE
-- The source repository modelled corporate actions as two tables,
-- target.dividend_events and target.stock_split_events, populated from one
-- staging table by filtering on action type. This framework keeps one staging
-- table to one target table throughout, so the two are combined here into a
-- single table carrying an action_type discriminator. The column set is the
-- union of the two originals; no column was invented and none was dropped.
--
-- Fields not applicable to a given action type are left NULL: a DIVIDEND row
-- has no split_ratio, a SPLIT row has no amount_per_unit or payment_date.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${PROD_SCHEMA}.corporate_actions (
    corporate_action_id    BIGSERIAL PRIMARY KEY,
    company_id             BIGINT,
    action_code            VARCHAR(20),
    action_type            VARCHAR(20),
    ex_date                DATE,
    record_date            DATE,
    payment_date           DATE,
    effective_date         DATE,
    drp_price              NUMERIC(18,4),
    amount_per_unit        NUMERIC(18,4),
    split_ratio            NUMERIC(18,4)
);


-- ---------------------------------------------------------------------------
-- 4. share_registry_transactions  <- XYZ-Shareholder_Transactions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${PROD_SCHEMA}.share_registry_transactions (
    transaction_id            BIGSERIAL PRIMARY KEY,
    company_id                BIGINT,
    shareholder_id            BIGINT,
    source_transaction_id     VARCHAR(20),
    transaction_type          VARCHAR(10),
    units                     NUMERIC(20,4),
    transaction_date          DATE,
    transaction_description   VARCHAR(100)
);


-- ---------------------------------------------------------------------------
-- 5. holdings               <- XYZ-Shareholder_Holdings
--
-- NOTE ON PROVENANCE
-- The source repository did not contain a production holdings TABLE. Holdings
-- existed only as stg.holdings_snapshot (staging) and
-- target.vw_reconstructed_holdings (a VIEW). The table below was derived from
-- that view's column set, applied to the production conventions used by the
-- other target tables in that repository: surrogate BIGSERIAL primary key,
-- BIGINT foreign keys, VARCHAR codes, NUMERIC(20,4) unit quantities.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ${PROD_SCHEMA}.holdings (
    holding_id       BIGSERIAL PRIMARY KEY,
    company_id       BIGINT,
    shareholder_id   BIGINT,
    identifier       VARCHAR(20),
    units            NUMERIC(20,4),
    as_of_date       DATE
);
