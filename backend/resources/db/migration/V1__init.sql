-- Flyway migration (OceanBase / MySQL dialect). All DDL lives here, never in code.
--
-- tx_events: one row per analyze/apply, for a lightweight audit trail. We record what
-- was applied, never the workbook or the screenshot themselves.
-- tx_seen:   fingerprints of transactions already applied, so a re-uploaded screenshot
-- that overlaps earlier work raises a warning instead of being double-counted.

CREATE TABLE tx_events (
    id         BIGINT       NOT NULL AUTO_INCREMENT,
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actor      VARCHAR(320) NOT NULL DEFAULT 'anonymous',
    action     VARCHAR(32)  NOT NULL,
    sheet      VARCHAR(128) NULL,
    cell       VARCHAR(16)  NULL,
    count      INT          NOT NULL DEFAULT 0,
    detail     VARCHAR(512) NULL,
    PRIMARY KEY (id)
) DEFAULT CHARSET=utf8mb4;

CREATE TABLE tx_seen (
    fingerprint CHAR(64)      NOT NULL,
    tx_date     VARCHAR(32)   NULL,
    amount      DECIMAL(14,4) NOT NULL,
    seen_at     DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (fingerprint)
) DEFAULT CHARSET=utf8mb4;