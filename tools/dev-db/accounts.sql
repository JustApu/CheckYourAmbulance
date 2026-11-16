-- Development database and account, created once when the compose volume is
-- first initialized. Synthetic credentials for local use only.
-- cya_owner is the schema-owner account (SPEC 17.7 item 1): it runs migrations
-- and tests, and may create/drop the test databases. The least-privilege runtime
-- account and its grants are defined by the audit-log issue (issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23)).
CREATE DATABASE IF NOT EXISTS cya CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'cya_owner'@'%' IDENTIFIED BY 'dev-only-owner-password';
GRANT ALL PRIVILEGES ON cya.* TO 'cya_owner'@'%';
GRANT ALL PRIVILEGES ON test_cya.* TO 'cya_owner'@'%';
-- Every other test database: the clones `manage.py test --parallel N` makes
-- (test_cya_1 to test_cya_N) and the CYA_DB_TEST_NAME names of concurrent runs
-- (issue 158). `\_` matches a literal underscore.
GRANT ALL PRIVILEGES ON `test\_cya%`.* TO 'cya_owner'@'%';
