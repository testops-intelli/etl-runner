-- Reference data.
--
-- Lookup targets are prerequisites of a migration, not outputs of it. They are
-- seeded here from standard code lists and maintained independently.
--
-- They are deliberately NOT built by running SELECT DISTINCT over a client
-- extract. Reference data assembled from whatever values happened to appear in
-- one client's file is not reference data: it silently blesses that client's
-- typos as valid codes, and it cannot detect an unrecognised code in the next
-- client's file because every code it has ever seen is by definition valid.
-- Seeding independently is what allows a LOOKUP miss to mean something.
--
-- ${REF_SCHEMA} is substituted from the REF_SCHEMA setting in .env.
-- Extend these lists as the migration scope requires.

CREATE TABLE IF NOT EXISTS ${REF_SCHEMA}.currency (
    currency_id     SERIAL PRIMARY KEY,
    currency_code   VARCHAR(3) NOT NULL UNIQUE,
    currency_name   TEXT
);

CREATE TABLE IF NOT EXISTS ${REF_SCHEMA}.country (
    country_id      SERIAL PRIMARY KEY,
    country_code    VARCHAR(10) NOT NULL UNIQUE,
    country_name    TEXT
);

INSERT INTO ${REF_SCHEMA}.currency (currency_code, currency_name) VALUES
    ('AUD', 'Australian Dollar'),
    ('NZD', 'New Zealand Dollar'),
    ('SGD', 'Singapore Dollar'),
    ('GBP', 'Pound Sterling'),
    ('USD', 'United States Dollar'),
    ('EUR', 'Euro'),
    ('HKD', 'Hong Kong Dollar'),
    ('JPY', 'Japanese Yen')
ON CONFLICT (currency_code) DO NOTHING;

INSERT INTO ${REF_SCHEMA}.country (country_code, country_name) VALUES
    ('AU', 'Australia'),
    ('NZ', 'New Zealand'),
    ('SG', 'Singapore'),
    ('UK', 'United Kingdom'),
    ('US', 'United States'),
    ('HK', 'Hong Kong'),
    ('JP', 'Japan'),
    ('IE', 'Ireland')
ON CONFLICT (country_code) DO NOTHING;
