-- THROWAWAY spike schema (issue 15 (https://github.com/JustApu/CheckYourAmbulance-archive/issues/15)). A tiny subset of SPEC section 18 used only to
-- exercise database behavior. Not the real schema (that is the M1 contracts issue).
-- Synthetic data only. Works on MariaDB 10.11 and MySQL 8.0 (verified by the tests).

CREATE TABLE users (
  user_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  idp_subject  VARCHAR(64)  NOT NULL,
  display_name VARCHAR(32)  NOT NULL,
  UNIQUE KEY uq_users_subject (idp_subject)
) ENGINE=InnoDB;

CREATE TABLE vehicles (
  vehicle_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name       VARCHAR(32) NOT NULL
) ENGINE=InnoDB;

CREATE TABLE items (
  item_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  name    VARCHAR(64) NOT NULL
) ENGINE=InnoDB;

-- Append-only (SPEC 7.2, 18). The application account gets SELECT, INSERT only.
CREATE TABLE check_record (
  check_id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  operation_id          CHAR(36)   NOT NULL,
  vehicle_id            BIGINT UNSIGNED NOT NULL,
  item_id               BIGINT UNSIGNED NOT NULL,
  outcome               ENUM('PASSED','FAILED','UNABLE','NA') NOT NULL,
  explanation           VARCHAR(120) NULL,
  actor_user_id         BIGINT UNSIGNED NOT NULL,
  basis_check_id        BIGINT UNSIGNED NULL,
  acknowledges_check_id BIGINT UNSIGNED NULL,
  server_recorded_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uq_check_operation (operation_id),
  KEY ix_check_item (vehicle_id, item_id, check_id),
  CONSTRAINT ck_unable_needs_reason CHECK (outcome <> 'UNABLE' OR explanation IS NOT NULL),
  CONSTRAINT fk_check_vehicle FOREIGN KEY (vehicle_id) REFERENCES vehicles (vehicle_id),
  CONSTRAINT fk_check_item    FOREIGN KEY (item_id)    REFERENCES items (item_id),
  CONSTRAINT fk_check_actor   FOREIGN KEY (actor_user_id) REFERENCES users (user_id)
) ENGINE=InnoDB;

-- Append-only: corrections and invalidations (SPEC 7.7, 7.8).
CREATE TABLE check_event (
  event_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  operation_id  CHAR(36) NOT NULL,
  check_id      BIGINT UNSIGNED NOT NULL,
  event_type    ENUM('CORRECTION','INVALIDATION') NOT NULL,
  new_outcome   ENUM('PASSED','FAILED','UNABLE','NA') NULL,
  explanation   VARCHAR(120) NULL,
  actor_user_id BIGINT UNSIGNED NOT NULL,
  recorded_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  UNIQUE KEY uq_event_operation (operation_id),
  CONSTRAINT ck_invalidation_needs_reason CHECK (event_type <> 'INVALIDATION' OR (explanation IS NOT NULL AND CHAR_LENGTH(explanation) > 0)),
  CONSTRAINT fk_event_check FOREIGN KEY (check_id) REFERENCES check_record (check_id),
  CONSTRAINT fk_event_actor FOREIGN KEY (actor_user_id) REFERENCES users (user_id)
) ENGINE=InnoDB;

-- Append-only audit trail (SPEC 16.1).
CREATE TABLE audit_event (
  audit_id    BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  actor_id    BIGINT UNSIGNED NULL,
  action      VARCHAR(40) NOT NULL,
  target      VARCHAR(64) NULL,
  detail      VARCHAR(200) NULL,
  recorded_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;

-- Mutable serialization row: one per (vehicle, item). Taking its row lock serializes
-- concurrent writers for that item only (SPEC CON-5).
CREATE TABLE item_lock (
  vehicle_id BIGINT UNSIGNED NOT NULL,
  item_id    BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (vehicle_id, item_id)
) ENGINE=InnoDB;

-- Obligations (SPEC 5.1, 5.8): idempotent on (vehicle, item, due instant, requirement version).
CREATE TABLE obligation (
  obligation_id  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  vehicle_id     BIGINT UNSIGNED NOT NULL,
  item_id        BIGINT UNSIGNED NOT NULL,
  due_at_utc     DATETIME(6) NOT NULL,
  early_at_utc   DATETIME(6) NOT NULL,
  grace_at_utc   DATETIME(6) NOT NULL,
  requirement_version INT NOT NULL,
  UNIQUE KEY uq_obligation (vehicle_id, item_id, due_at_utc, requirement_version)
) ENGINE=InnoDB;

-- Defense in depth: triggers reject UPDATE/DELETE on append-only tables even for
-- accounts that hold the privilege.
DELIMITER //
CREATE TRIGGER check_record_no_update BEFORE UPDATE ON check_record FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check_record is append-only'//
CREATE TRIGGER check_record_no_delete BEFORE DELETE ON check_record FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check_record is append-only'//
CREATE TRIGGER check_event_no_update BEFORE UPDATE ON check_event FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check_event is append-only'//
CREATE TRIGGER check_event_no_delete BEFORE DELETE ON check_event FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'check_event is append-only'//
CREATE TRIGGER audit_event_no_update BEFORE UPDATE ON audit_event FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_event is append-only'//
CREATE TRIGGER audit_event_no_delete BEFORE DELETE ON audit_event FOR EACH ROW
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_event is append-only'//
-- COR-1: only the original author may correct a check (enforced at the database too).
CREATE TRIGGER check_event_author_rule BEFORE INSERT ON check_event FOR EACH ROW
BEGIN
  IF NEW.event_type = 'CORRECTION' AND NEW.actor_user_id <>
     (SELECT actor_user_id FROM check_record WHERE check_id = NEW.check_id) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'only the original author may correct';
  END IF;
END//
DELIMITER ;

-- Least-privilege application account (SPEC ARC-2). Synthetic local password.
CREATE USER IF NOT EXISTS 'app'@'localhost' IDENTIFIED BY 'spike-local-only';
GRANT SELECT, INSERT ON {DB}.check_record TO 'app'@'localhost';
GRANT SELECT, INSERT ON {DB}.check_event  TO 'app'@'localhost';
GRANT SELECT, INSERT ON {DB}.audit_event  TO 'app'@'localhost';
GRANT SELECT, INSERT ON {DB}.obligation   TO 'app'@'localhost';
-- UPDATE is needed here only: MySQL 8 requires UPDATE/DELETE/LOCK TABLES for SELECT ... FOR UPDATE.
-- item_lock holds no business data (it is a lock anchor), so this does not weaken immutability.
GRANT SELECT, INSERT, UPDATE ON {DB}.item_lock TO 'app'@'localhost';
GRANT SELECT, INSERT ON {DB}.users        TO 'app'@'localhost';
GRANT SELECT          ON {DB}.vehicles    TO 'app'@'localhost';
GRANT SELECT          ON {DB}.items       TO 'app'@'localhost';
