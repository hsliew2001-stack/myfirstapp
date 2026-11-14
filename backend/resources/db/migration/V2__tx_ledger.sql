-- Flyway migration (OceanBase / MySQL dialect). All DDL lives here, never in code.
--
-- tx_ledger: the per-user transaction history that recurring-pattern detection and the
-- forward forecast are computed from. One row per applied or imported transaction.
--
-- Why a separate table from tx_seen: tx_seen is a *global* fingerprint set used only to
-- warn about a re-uploaded screenshot. It has no owner, no date and no description, so it
-- cannot answer "which bills repeat" or "what will my balance be on the 27th". Nothing
-- here is derived — recurring series are computed on read and never stored.

CREATE TABLE tx_ledger (
    id          BIGINT        NOT NULL AUTO_INCREMENT,
    -- x-forwarded-email when SSO is on; 'anonymous' otherwise. Every query filters on it.
    actor       VARCHAR(320)  NOT NULL,
    -- The account label the user typed ("UOB One"), so per-account views are possible.
    account     VARCHAR(128)  NOT NULL,
    tx_date     DATE          NOT NULL,
    description VARCHAR(512)  NOT NULL,
    -- Signed: money leaving the account is negative. DECIMAL, never a float.
    amount      DECIMAL(14,4) NOT NULL,
    currency    CHAR(3)       NOT NULL DEFAULT 'SGD',
    -- from workbook.fingerprint(); makes an import of overlapping rows a no-op.
    fingerprint CHAR(64)      NOT NULL,
    source      VARCHAR(16)   NOT NULL DEFAULT 'screenshot',
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_ledger_actor_fp (actor, fingerprint),
    KEY ix_ledger_scope (actor, account, currency, tx_date)
) DEFAULT CHARSET=utf8mb4;