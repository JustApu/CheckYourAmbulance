-- Development database and account, created once when the compose volume is
-- first initialized. Synthetic credentials for local use only.
-- cya_owner is the schema-owner account (SPEC 17.7 item 1): it runs migrations
-- and tests, and may create/drop the test database. The least-privilege runtime
-- account and its grants are defined by the audit-log issue (issue 23 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/23)).
CREATE DATABASE IF NOT EXISTS cya CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'cya_owner'@'%' IDENTIFIED BY 'dev-only-owner-password';
GRANT ALL PRIVILEGES ON cya.* TO 'cya_owner'@'%';
GRANT ALL PRIVILEGES ON test_cya.* TO 'cya_owner'@'%';
