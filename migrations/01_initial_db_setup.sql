-- 01_initial_db_setup.sql
--
-- Initial schema for the market-data-service.
--
-- Creates the two tables used by the Tortoise ORM models in
-- app/database/models/:
--   * etf_constituents  — ETF holdings cache (SPY, QQQ, IWM, ...)
--   * market_bars       — Cached daily OHLCV bars from Polygon.io
--
-- This script is idempotent: every CREATE uses IF NOT EXISTS so it can be
-- re-run safely against a partially initialized database.
--
-- Run against a MySQL 8.x instance:
--   mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER" -p \
--         "$MYSQL_DATABASE" < migrations/01_initial_db_setup.sql

-- ---------------------------------------------------------------------------
-- etf_constituents
-- ---------------------------------------------------------------------------
-- One row per holding of an ETF. Rows are replaced wholesale whenever a
-- new snapshot is fetched (see ETFConstituentsRepository.save), so we don't
-- keep historical snapshots here — fetched_at records the snapshot date.

CREATE TABLE IF NOT EXISTS etf_constituents (
    id            INT          NOT NULL AUTO_INCREMENT,
    etf_symbol    VARCHAR(24)  NOT NULL,
    ticker        VARCHAR(24)  NOT NULL,
    name          VARCHAR(255) NOT NULL,
    weight        DOUBLE       NOT NULL,
    fetched_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX idx_etf_symbol (etf_symbol),
    INDEX idx_etf_ticker (etf_symbol, ticker)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- market_bars
-- ---------------------------------------------------------------------------
-- One row per daily OHLCV bar. `timestamp` is the bar open time in epoch
-- milliseconds (UTC midnight for daily bars); it is the only time field —
-- no separate date column is needed because the day is trivially
-- derivable from the timestamp.

CREATE TABLE IF NOT EXISTS market_bars (
    id            BIGINT       NOT NULL AUTO_INCREMENT,
    ticker        VARCHAR(20)  NOT NULL,
    timestamp     BIGINT       NOT NULL,
    open          DOUBLE       NOT NULL,
    high          DOUBLE       NOT NULL,
    low           DOUBLE       NOT NULL,
    close         DOUBLE       NOT NULL,
    volume        DOUBLE       NOT NULL,
    vwap          DOUBLE       NULL,
    trade_count   INT          NULL,
    fetched_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_bar_identity (ticker, timestamp),
    INDEX idx_bar_ticker_timestamp (ticker, timestamp)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;