# MAPPING BOOK

Wizard answer sheets for the five mappings in the demo chain.

The mapping wizard asks a lot of questions and the right answers are not
guessable from the prompts alone — which of thirteen source columns feeds
`communication_preference` is a decision about the data, not something the tool
can infer. This file records those decisions so a mapping can be reproduced
without rediscovering them.

Every sheet here matches the mappings exercised by `verify_engine.py`, so they
are verified rather than remembered.

---

## Before you start

**Answer with names, not numbers.** Every list prompt accepts either the
number or the name itself. The numbers shift as tables are added to a schema or
as list ordering changes; the names do not. Every sheet below gives names.

**Navigation.** At any prompt while defining a column, `redo` restarts the
current column and `back` steps to the previous one. Both must be typed in
full. Navigation is disabled while entering value-map targets, because there
every keystroke is data — `redo` there means the literal string `redo`.

**Nothing is written until you confirm the summary.** Abandoning a mapping
midway leaves no trace, so a botched run costs time and nothing else.

**Value maps are prompted in the order the wizard finds them**, which is
alphabetical by source value. The sheets below list them in that order, so you
can answer straight down.

**Load order matters.** Each mapping resolves surrogate keys issued by an
earlier one. Work through the sections in the order given.

---

## Getting your company_id

Three mappings need the surrogate key issued by `company_master`. After
running mapping 1, get it with:

```sql
SELECT company_id FROM prod.company_master;
```

It is `1` on a freshly built environment. Below it is written `<company_id>`.

---

## 1. company_master

**Ingest**

```
python scripts/ingest.py
```

| Prompt | Answer |
|--------|--------|
| Select a file | `XYZ-Company_Static_Data_2025-12-31.xlsx` |
| Use the file name for the staging table? | `n` |
| Staging table name | `company_static_data` |
| Confirm name, then proceed | `y`, `y` |

Expect `1/1 rows loaded`, and a note that `Shares Outstanding` was normalized
to `shares_outstanding`.

**Map**

```
python scripts/map_wizard.py
```

| Prompt | Answer |
|--------|--------|
| Source staging table | `company_static_data` |
| Target production table | `company_master` |
| Mapping name | `company_to_prod` |
| Row filter | *(blank)* |

| # | Target column | Rule | Source column |
|---|---------------|------|---------------|
| 1 | company_name | `1` DIRECT | `company_name` |
| 2 | ticker | `1` DIRECT | `ticker` |
| 3 | exchange_code | `1` DIRECT | `exchange` |
| 4 | isin | `1` DIRECT | `isin` |
| 5 | sedol | `6` NULL | — |
| 6 | currency_id | `3` LOOKUP | `currency` |
| 7 | shares_outstanding | `1` DIRECT | `shares_outstanding` |
| 8 | company_status | `2` VALUE_MAP | `status` |
| 9 | listing_date | `1` DIRECT | `listing_date` |

*Column 6 lookup:* schema `ref`, table `currency`, match column
`currency_code`, return column `currency_id`, fallback `n`.

*Column 8 value map:*

```
'Active' ->  A
```
then fallback `n`.

**Expected discards:** `internal_identifier`

**Run**

```
python scripts/etl_runner.py --mapping company_to_prod
```

Expect `PASS`, `1/1`.

---

## 2. shareholder_master

The heaviest mapping in the chain: ten columns, two reference lookups and four
value maps.

**Ingest**

| Prompt | Answer |
|--------|--------|
| Select a file | `XYZ-Shareholders_Static_Data_2025-12-31.xlsx` |
| Use the file name for the staging table? | `n` |
| Staging table name | `shareholders_static_data` |

Expect `100/100 rows loaded`, plus notices that
`Communication Preference` was normalized and that the duplicate `Holder_ID`
header was renamed to `holder_id_2` rather than dropped.

**Map**

| Prompt | Answer |
|--------|--------|
| Source staging table | `shareholders_static_data` |
| Target production table | `shareholder_master` |
| Mapping name | `shareholders_to_prod` |
| Row filter | *(blank)* |

| # | Target column | Rule | Source column |
|---|---------------|------|---------------|
| 1 | company_id | `4` CONSTANT | value `<company_id>` |
| 2 | shareholder_name | `1` DIRECT | `holder_name` |
| 3 | shareholder_type | `2` VALUE_MAP | `holder_type` |
| 4 | residency_country_id | `3` LOOKUP | `residency_country` |
| 5 | currency_id | `3` LOOKUP | `currency` |
| 6 | shareholder_status | `2` VALUE_MAP | `holder_status` |
| 7 | holder_reference_type | `1` DIRECT | `holder_reference_type` |
| 8 | holder_reference_number | `1` DIRECT | `holder_reference_number` |
| 9 | communication_preference | `2` VALUE_MAP | `communication_preference` |
| 10 | holder_category | `2` VALUE_MAP | `holder_category` |

*Column 4 lookup:* schema `ref`, table `country`, match `country_code`,
return `country_id`, fallback `n`.

*Column 5 lookup:* schema `ref`, table `currency`, match `currency_code`,
return `currency_id`, fallback `n`.

*Value maps, in prompt order:*

```
col 3  shareholder_type          col 6  shareholder_status
  'FUND'          ->  F            'ACTIVE'     ->  A
  'INDIVIDUAL'    ->  I            'CLOSED'     ->  C
  'JOINT'         ->  J            'DECEASED'   ->  D
  'SMSF'          ->  S            'DORMANT'    ->  O
  'TRUST'         ->  T            'SUSPENDED'  ->  S

col 9  communication_preference  col 10  holder_category
  'EMAIL'         ->  E            'FOUNDATION'     ->  F
  'POST'          ->  P            'INSTITUTIONAL'  ->  I
                                   'RETAIL'         ->  R
```

Fallback `n` after each.

> `holder_category` contains `FOUNDATION`. The original repository's ETL mapped
> `FOUNDER`, a value the data never contains, so every foundation holder fell
> through to a catch-all unnoticed. Leave `FOUNDATION` blank here and the run
> fails at row 98 and names the value. That is the intended behaviour, not a
> defect.

**Expected discards:** `holder_id`, `holder_id_2`, `drp_flag`,
`initial_allocation_flag` — four of them. A fifth means something was
mismapped.

**Run**

```
python scripts/etl_runner.py --mapping shareholders_to_prod
```

Expect `PASS`, `100/100`.

---

## 3. corporate_actions

One target table carrying both action types. Fields that do not apply to an
action type are left NULL by the source data itself: a DIVIDEND row has no
split ratio, a SPLIT row no per-unit amount.

**Ingest**

| Prompt | Answer |
|--------|--------|
| Select a file | `XYZ-Corporate_Actions-2025-12-31.xlsx` |
| Use the file name for the staging table? | `n` |
| Staging table name | `corporate_actions` |

Expect `3/3 rows loaded`, with `$ per unit` normalized to `amount_per_unit`
and `stock split ratio` to `stock_split_ratio`.

**Map**

| Prompt | Answer |
|--------|--------|
| Source staging table | `corporate_actions` |
| Target production table | `corporate_actions` |
| Mapping name | `corporate_actions_to_prod` |
| Row filter | *(blank)* |

| # | Target column | Rule | Source column |
|---|---------------|------|---------------|
| 1 | company_id | `4` CONSTANT | value `<company_id>` |
| 2 | action_code | `5` EXPRESSION | template `CA_{action_id}` |
| 3 | action_type | `1` DIRECT | `type` |
| 4 | ex_date | `1` DIRECT | `ex_date` |
| 5 | record_date | `1` DIRECT | `record_date` |
| 6 | payment_date | `1` DIRECT | `payment_date` |
| 7 | effective_date | `1` DIRECT | `effective_date` |
| 8 | drp_price | `1` DIRECT | `drp_price` |
| 9 | amount_per_unit | `1` DIRECT | `amount_per_unit` |
| 10 | split_ratio | `1` DIRECT | `stock_split_ratio` |

*Column 2 expression:* type the template exactly, braces included:
`CA_{action_id}`. It substitutes source column values into a string; it does
not compute. Produces `CA_CA001`, `CA_CA002`, `CA_CA003`.

**Expected discards:** none — every source column is consumed.

**Run**

```
python scripts/etl_runner.py --mapping corporate_actions_to_prod
```

Expect `PASS`, `3/3`.

---

## 4. share_registry_transactions

**Ingest**

| Prompt | Answer |
|--------|--------|
| Select a file | `XYZ-Shareholder_Transactions-2025-12-31.xlsx` |
| Use the file name for the staging table? | `n` |
| Staging table name | `transactions` |

Expect `128/128 rows loaded`.

**Map**

| Prompt | Answer |
|--------|--------|
| Source staging table | `transactions` |
| Target production table | `share_registry_transactions` |
| Mapping name | `transactions_to_prod` |
| Row filter | *(blank)* |

| # | Target column | Rule | Source column |
|---|---------------|------|---------------|
| 1 | company_id | `4` CONSTANT | value `<company_id>` |
| 2 | shareholder_id | `3` LOOKUP | `holder` |
| 3 | source_transaction_id | `1` DIRECT | `txn_id` |
| 4 | transaction_type | `1` DIRECT | `type` |
| 5 | units | `1` DIRECT | `units` |
| 6 | transaction_date | `1` DIRECT | `date` |
| 7 | transaction_description | `1` DIRECT | `description` |

*Column 2 lookup:* schema `prod`, table `shareholder_master`, match
`shareholder_name`, return `shareholder_id`, fallback `n`.

Requires mapping 2 to have run first. Matching is on holder name and is
case-insensitive.

**Expected discards:** none.

**Run**

```
python scripts/etl_runner.py --mapping transactions_to_prod
```

Expect `PASS`, `128/128`.

---

## 5. holdings

Both foreign keys resolved by lookup — no constants.

**Ingest**

| Prompt | Answer |
|--------|--------|
| Select a file | `XYZ-Shareholder_Holdings_2025-12-31.xlsx` |
| Use the file name for the staging table? | `n` |
| Staging table name | `holdings` |

Expect `31/31 rows loaded`.

**Map**

| Prompt | Answer |
|--------|--------|
| Source staging table | `holdings` |
| Target production table | `holdings` |
| Mapping name | `holdings_to_prod` |
| Row filter | *(blank)* |

| # | Target column | Rule | Source column |
|---|---------------|------|---------------|
| 1 | company_id | `3` LOOKUP | `identifier` |
| 2 | shareholder_id | `3` LOOKUP | `holder` |
| 3 | identifier | `1` DIRECT | `identifier` |
| 4 | units | `1` DIRECT | `units` |
| 5 | as_of_date | `1` DIRECT | `as_of_date` |

*Column 1 lookup:* schema `prod`, table `company_master`, match `isin`,
return `company_id`, fallback `n`. The holdings extract carries the security
identifier, which is the company's ISIN, so `company_id` resolves rather than
being asserted as a constant.

*Column 2 lookup:* schema `prod`, table `shareholder_master`, match
`shareholder_name`, return `shareholder_id`, fallback `n`.

**Expected discards:** `holder_id`

**Run**

```
python scripts/etl_runner.py --mapping holdings_to_prod
```

Expect `PASS`, `31/31`.

---

## Checking the finished chain

```sql
SELECT 'company_master' AS target_table, COUNT(*) FROM prod.company_master
UNION ALL SELECT 'shareholder_master', COUNT(*) FROM prod.shareholder_master
UNION ALL SELECT 'corporate_actions', COUNT(*) FROM prod.corporate_actions
UNION ALL SELECT 'share_registry_transactions',
                 COUNT(*) FROM prod.share_registry_transactions
UNION ALL SELECT 'holdings', COUNT(*) FROM prod.holdings;
```

Expect 1, 100, 3, 128 and 31.

Both unit totals should equal the company's shares outstanding, 1,535,000:

```sql
SELECT (SELECT SUM(units) FROM prod.share_registry_transactions) AS ledger,
       (SELECT SUM(units) FROM prod.holdings)                    AS snapshot,
       (SELECT shares_outstanding FROM prod.company_master)      AS outstanding;
```

More queries in `sql/QUERYBOOK.sql`.

---

## Notes on the choices above

**Why `company_id` is a CONSTANT in mappings 2, 3 and 4.** Those extracts carry
no company reference at all, so there is nothing to resolve against. A constant
is the honest answer for a single-company migration. The point is that it lives
in the registry as configuration rather than as a literal compiled into the
transformation — which is what the original repository did, with `101` written
into five separate SQL files.

**Why fallbacks are always `n`.** A fallback catches values absent from the map,
which sounds safe and is the opposite. It converts an unrecognised code into a
silent default; without one the run fails and names the value. The `FOUNDATION`
case is exactly this failure mode surviving unnoticed in production.

**Why staging columns are all TEXT.** A raw extract should land losslessly. A
malformed date should not abort ingestion; it should fail at transform time,
attributed to a row and a column. That is why `listing_date` is `1` DIRECT and
not something more elaborate: the cast to `DATE` is driven by the target
column's own type.
