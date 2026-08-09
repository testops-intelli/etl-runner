# ETL Migration Framework

A metadata-driven ETL runner for data migration rehearsal. A source file is
ingested into a staging table generated from the file's own header row; a
wizard records how that staging table maps onto a pre-existing production
table; a runner applies the mapping and reports whether every row landed.

No table name, column name, constant or code translation is written into the
Python or SQL source. All of it lives as rows in a metadata registry, which is
what allows one runner to serve any staging/production table pair.

---

## Scope

This framework covers **ingestion, mapping and transfer**. Its success
criterion is completeness: every source row inserted, zero failures.

It does **not** perform semantic or numerical validation. Confirming that the
migrated values are *correct* — aggregate reconciliation, row-level field
comparison, referential integrity across the target — is a separate concern
and a separate script, not yet built. A PASS from the runner means the data
moved, not that the data is right.

---

## Pipeline

```
source_files/client_extract.xlsx
        |
        |  scripts/ingest.py        convert to CSV, normalize headers,
        v                           create staging table, load
stage.<table>                       (all columns TEXT)
        |
        |  scripts/map_wizard.py    per target column, record a rule
        v                           in the metadata registry
etl_meta.mapping_set / column_rule / value_map
        |
        |  scripts/etl_runner.py    apply rules, cast, insert
        v
prod.<table>                        (pre-existing target contract)
```

### Load order

Target tables form a chain. Each extract is loaded in the order that makes the
keys it needs already exist:

```
company_master   surrogate company_id issued here
      |
      v
holdings         company_id resolved by LOOKUP on ISIN
```

The runner does not model this dependency. It fails loudly on an unresolved
key rather than inserting a NULL, so the order is the operator's to follow.
Modelling it is listed under extension points.

---

## Setup

### 1. Install

**Windows (PowerShell)**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**macOS / Linux (bash)**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure credentials

Copy the example file and edit it. `.env` is gitignored; `.env.example` is
committed.

**Windows (PowerShell)**

```powershell
Copy-Item .env.example .env
notepad .env
```

**macOS / Linux (bash)**

```bash
cp .env.example .env
```

`PGUSER` must be permitted to run `CREATE DATABASE`. `PGADMIN_DB` is an
existing database used only to issue that statement — normally `postgres`.

### 3. Build the environment

```
python scripts/create_env.py
```

This creates the database, the `stage`, `prod` and `etl_meta` schemas, the
production tables from `sql/prod_tables.sql`, and the metadata registry.

`--reset` drops the database first and requires typing the database name to
confirm.

---

## Usage

### Ingest a source file

```
python scripts/ingest.py
```

The wizard lists the files in `source_files/`, converts `.xlsx` to `.csv` if
required (same file stem retained), and asks whether the staging table should
take the file's name or one you supply.

**Staging columns are created as TEXT.** A raw client extract should land
losslessly; a malformed date should not abort ingestion. Type conversion is a
mapping decision applied later, where a failure can be attributed to a specific
row and column instead of aborting a bulk load with no indication of which
record was bad.

Header rows from real extracts are rarely SQL-safe. Headers are normalized to
snake_case, and every change is printed rather than made silently:

```
Column names adjusted for SQL safety:
  - column 10: 'Communication Preference' normalized to 'communication_preference'
  - column 13: duplicate header 'Holder_ID' renamed to 'holder_id_2' (no column dropped)
```

Duplicate headers are suffixed, never dropped.

### Define a mapping

```
python scripts/map_wizard.py
```

The wizard is driven by the **target** table. Production is the contract, so
every target column is walked in turn and must be given a rule. Columns the
database generates (identity / serial) are skipped automatically.

| Rule type    | Behaviour |
|--------------|-----------|
| `DIRECT`     | Copy a source column, cast to the target column's type |
| `VALUE_MAP`  | Coded translation, e.g. `ACTIVE` → `A` |
| `LOOKUP`     | Resolve a code or name to a key in another table |
| `CONSTANT`   | One fixed value for every row |
| `EXPRESSION` | Template over source columns, e.g. `D_{action_id}` |
| `NULL`       | Deliberately empty |

For `VALUE_MAP`, the wizard reads the distinct values actually present in the
staging column and prompts for each, so the map is built against real data
rather than assumption.

At any prompt while defining a column, `redo` restarts the current column and
`back` steps to the previous one. The rule type prompt has no default: pressing
Enter re-asks rather than selecting a rule, because a wrong rule should never be
the cheapest keystroke available. Nothing is written to the registry until the
summary is confirmed, so abandoning a mapping midway leaves no trace.

Source columns no rule claims are listed at the end and recorded in
`etl_meta.discarded_source_column`. A discarded column is a decision on the
record, not an omission nobody noticed.

A mapping is stored under a name, and an optional row filter restricts which
staging rows it consumes. One staging table can therefore feed several targets
under different mapping names — a corporate actions extract splitting into
dividend and stock split targets, for example.

### Run the ETL

```
python scripts/etl_runner.py --mapping holdings_to_prod
python scripts/etl_runner.py --list
```

---

## Failure behaviour

**The run is one transaction, all or nothing.** A row that cannot be
transformed does not stop the run — it is recorded and processing continues, so
every failing row is reported in a single pass. If any row failed, the whole
transaction is rolled back and production is left untouched. A half-migrated
target is worse than an unmigrated one.

```
ETL RESULT: FAIL
Rows that would have inserted: 29/31
Rows failed: 2

The transaction was rolled back. prod.holdings is unchanged.

Failing rows:
  row 30     column units                cannot cast 'not-a-number' to numeric
  row 31     column as_of_date           cannot cast '31/13/2025' to date
```

**Unmapped values fail by default.** If a `VALUE_MAP` meets a value it has no
entry for, or a `LOOKUP` cannot resolve a key, the row fails and names the
offending value:

```
  row 98     column holder_category      value 'FOUNDATION' has no entry in the
                                         value map and no fallback is configured
```

A fallback is available per rule but is opt-in, never the default. Silently
defaulting unrecognised values to a catch-all is how a mapping gap survives a
migration unnoticed.

Every run is recorded in `etl_meta.etl_run` with row counts and status,
including runs that failed and rolled back.

### Later batches: reusing a mapping

A mapping is bound to the staging table, not to the file that filled it. Re-ingest
a later batch of the same extract under the same staging table name and the
mapping still applies — the wizard is not involved at all:

```
python scripts/ingest.py                                   # same staging table name
python scripts/etl_runner.py --mapping shareholders_to_prod
```

Run the wizard on a pair that is already mapped and it says so, and offers:

| Option | Use it when |
|--------|-------------|
| **use** | The mapping is right as it stands. Exits with the runner command. |
| **revise** | Something changed. Walks the columns with current rules prefilled; Enter keeps each one. |
| **new** | The same staging table needs to feed the target differently, under another name. |

`revise` is the answer to a later batch containing a code the value map has
never seen. The run fails and names the value; revise the mapping, supply a
target for the new code, press Enter through everything else, and re-run.
Translations already in the map are carried forward even when the batch
currently staged contains none of those codes.

Two things this does not do. Inserts are append-only: there is no business key
and no upsert, so re-loading a full snapshot appends duplicates rather than
updating rows. And a source column renamed by the client breaks the mapping —
the runner rejects it before touching a row and names the column, but the
mapping has to be revised.

### Reproducing the demo chain

`MAPPING_BOOK.md` holds wizard answer sheets for all five mappings: which
source column feeds each target column, the exact value-map translations, the
lookup schema/table/match/return for every lookup, and the discards to expect
before saving. The wizard cannot infer these — which of thirteen source columns
feeds `communication_preference` is a decision about the data, not something a
tool derives — so they are written down rather than rediscovered each time.

Every sheet in it is verified by driving the wizard with those exact answers
and checking the resulting mapping and row counts.

### Inspecting what happened

`sql/QUERYBOOK.sql` holds ad hoc queries for looking at the state after an
ingestion or a run: what landed in staging, what the ETL produced with its
lookups resolved back to readable values, the full rule set behind any mapping,
and the run history including failed runs. Nothing in it is executed by the
framework.

```
psql -h localhost -U <user> -d etl_migration -f sql/QUERYBOOK.sql
```

Most people will paste individual blocks rather than run the file whole. The
staging queries reference `stage.company_static_data`; change that to whatever
you named your staging table.

---

## Metadata registry

| Table | Purpose |
|-------|---------|
| `mapping_set` | One named staging→production mapping, with optional row filter |
| `column_rule` | One rule per target column |
| `value_map` | Source→target value pairs for `VALUE_MAP` rules |
| `discarded_source_column` | Source columns deliberately not carried across |
| `ingestion_log` | Every file loaded, with row and column counts |
| `etl_run` | Every run, its counts, and its outcome |

---

## Production tables

`sql/prod_tables.sql` defines five target tables, one per source extract:

| # | Target table | Source extract | Issues / consumes |
|---|--------------|----------------|-------------------|
| 1 | `company_master` | Company_Static_Data | issues `company_id` |
| 2 | `shareholder_master` | Shareholders_Static_Data | issues `shareholder_id` |
| 3 | `corporate_actions` | Corporate_Actions | consumes `company_id` |
| 4 | `share_registry_transactions` | Shareholder_Transactions | consumes both |
| 5 | `holdings` | Shareholder_Holdings | consumes both |

They must be loaded in that order: each one resolves surrogate keys issued by
an earlier table. The runner fails loudly on an unresolved lookup rather than
writing a NULL, so it will tell you if the order is wrong, but it does not
enforce it.

`company_master`, `shareholder_master` and `share_registry_transactions` are
taken verbatim from the source repository's target schema. `corporate_actions`
combines that repository's `dividend_events` and `stock_split_events` into one
table with an `action_type` discriminator, keeping one staging table to one
target table throughout; the column set is the union of the two originals.
`holdings` is derived, as noted in the DDL.

`sql/reference_data.sql` defines and seeds `ref.currency` and `ref.country`.
Lookup targets are prerequisites of a migration, not outputs of it. They are
deliberately **not** built by running `SELECT DISTINCT` over a client extract:
reference data assembled that way silently blesses one client's typos as valid
codes, and can never detect an unrecognised code in the next client's file
because every code it has seen is by definition valid. Seeding independently is
what allows a LOOKUP miss to mean something.

Production tables are created by `create_env.py`, never by the ingestion or the
runner, because production structures exist independently of whatever a client
happens to send. Staging is the mirror image: created at ingestion time from
the file itself.

Additional target tables are added by editing `sql/prod_tables.sql` and
re-running `create_env.py`. Nothing in the runner or the wizard needs to change.

---

## Verified

Verified on PostgreSQL 16 against the source extracts in `source_files/`:

- `.xlsx` → `.csv` conversion, header normalization, duplicate-header suffixing
- Staging table creation and load: 1/1 company, 31/31 holdings,
  100/100 shareholders, 3/3 corporate actions
- The full five-table chain in load order: `company_master`,
  `shareholder_master`, `corporate_actions`, `share_registry_transactions`,
  `holdings` — 1, 100, 3, 128 and 31 rows respectively, every foreign key
  resolved by lookup, zero unresolved
- Transaction ledger units and holdings snapshot units both total 1,535,000,
  matching the shares outstanding on the company extract
- Company mapping exercising four rule types in one pass: `DIRECT`,
  `LOOKUP` (currency code to `ref.currency.currency_id`), `VALUE_MAP`
  (`Active` to `A`), `NULL` (sedol), plus a discarded source column
- All six rule types, including `LOOKUP` resolving holder names to surrogate
  keys with zero unresolved rows
- Row filter restricting a corporate actions extract to dividend rows only
- Failure path: bad numeric, unparseable date, unmapped `VALUE_MAP` value, and
  unresolvable `LOOKUP` key each fail their row, are reported by row and
  column, and leave production unchanged after rollback
- Multiple independent failures reported in a single pass rather than aborting
  on the first one
- Pre-flight validation: a mapping referencing a source column the extract does
  not contain is rejected before any row is processed
- Failed runs recorded in `etl_meta.etl_run` with zero rows inserted
- Migrated holdings total 1,535,000 units, matching the shares outstanding on
  the company extract

### Reproducing it

```
python verify_engine.py
```

That is the whole procedure. The harness needs nothing beyond a populated
`.env` — not even `create_env.py`. It creates its own scratch database named
after `ETL_DB` with a `_verify` suffix, seeds reference data and production
tables, ingests every extract in `source_files/`, registers the mappings,
runs 36 checks, and drops the database again. **The database named in `ETL_DB`
is never touched.**

It ingests through the same `etl/ingestion.py` the wizard uses and invokes
`scripts/etl_runner.py` as a subprocess, so it exercises the shipped code
path rather than a reimplementation of it.

The failure behaviour is the part worth trusting, and a successful run never
demonstrates it. That is what the harness is for.

---

## Extension points

Stated rather than implied — none of the following is implemented.

- **Semantic validation.** The runner checks completeness only. Aggregate
  reconciliation and row-level field comparison belong in a separate validator.
- **Set-based insertion.** Rows are inserted individually inside SAVEPOINTs so
  a database rejection can be attributed to the row that caused it. That trades
  throughput for failure attribution, which suits migration rehearsal. Volume
  work would want batching with a fallback to row-by-row on error.
- **Load ordering.** A mapping using `LOOKUP` requires its lookup target to be
  populated first. The runner fails loudly on an unresolved key but does not
  model dependencies between mappings; ordering is the operator's.
- **`EXPRESSION` scope.** Templates substitute source column values into a
  string. That covers concatenation-style derivations such as
  `D_{action_id}`. It does not cover computation: no arithmetic, no
  conditionals. A derivation like `units * split_ratio` needs a computed
  expression rule type, which is a new rule type rather than a change to this
  one.
- **Reference data.** Lookup targets are prerequisites, seeded independently.
  The framework does not build reference tables from whatever values happened
  to appear in one client extract.
- **PostgreSQL only.** The DDL and the `COPY`-based load are PostgreSQL
  specific.
