-- 区域月目标分期：表结构参考（MySQL 8.0 语法；运行时不执行，建表由 SQLAlchemy create_all 完成）

CREATE TABLE store_month_targets (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  store_id       BIGINT NOT NULL,
  month          DATE NOT NULL COMMENT '统一存当月 1 日',
  target_amount  DOUBLE NOT NULL DEFAULT 0,
  updated_by     VARCHAR(160) NOT NULL DEFAULT '',
  created_at     DATETIME NOT NULL,
  updated_at     DATETIME NOT NULL,
  CONSTRAINT uq_store_month_target UNIQUE (store_id, month),
  CONSTRAINT fk_smt_store FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE
);

CREATE TABLE store_region_transfers (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  store_id       BIGINT NOT NULL,
  from_region    VARCHAR(40) NOT NULL,
  to_region      VARCHAR(40) NOT NULL,
  effective_date DATE NOT NULL COMMENT '生效当天起计入新区域',
  reason         TEXT,
  created_by     VARCHAR(160) NOT NULL DEFAULT '',
  seed_key       VARCHAR(120) UNIQUE,
  created_at     DATETIME NOT NULL,
  CONSTRAINT uq_transfer_store_effective UNIQUE (store_id, effective_date),
  CONSTRAINT fk_transfer_store FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE,
  INDEX ix_transfer_effective (effective_date)
);

CREATE TABLE target_batches (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  batch_no       VARCHAR(40) NOT NULL UNIQUE,
  month          DATE NOT NULL,
  version        INT NOT NULL DEFAULT 1,
  status         VARCHAR(16) NOT NULL DEFAULT 'draft'
                 COMMENT 'draft/running/publishing/published/archived',
  prev_status    VARCHAR(16) NULL COMMENT '中断恢复用：进入中间态前的状态',
  attempt        INT NOT NULL DEFAULT 0,
  operator_email VARCHAR(160) NOT NULL DEFAULT 'system',
  store_count    INT NOT NULL DEFAULT 0,
  line_count     INT NOT NULL DEFAULT 0,
  total_target   DOUBLE NOT NULL DEFAULT 0,
  metrics        JSON,
  seed_key       VARCHAR(120) UNIQUE,
  started_at     DATETIME NOT NULL,
  finished_at    DATETIME,
  created_at     DATETIME NOT NULL,
  updated_at     DATETIME NOT NULL,
  CONSTRAINT uq_target_batch_month_version UNIQUE (month, version),
  INDEX ix_target_batches_status (status)
);

CREATE TABLE target_lines (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  batch_id       BIGINT NOT NULL,
  store_id       BIGINT NOT NULL,
  store_code     VARCHAR(40) NOT NULL,
  store_name     VARCHAR(120) NOT NULL,
  segment_no     INT NOT NULL,
  region         VARCHAR(40) NOT NULL,
  period_start   DATE NOT NULL,
  period_end     DATE NOT NULL,
  days           INT NOT NULL DEFAULT 0,
  target_cents   BIGINT NOT NULL DEFAULT 0 COMMENT '金额以分为准，保证逐层对平',
  target_amount  DOUBLE NOT NULL DEFAULT 0,
  transfer_id    BIGINT NULL,
  created_at     DATETIME NOT NULL,
  CONSTRAINT uq_target_line_segment UNIQUE (batch_id, store_id, segment_no),
  CONSTRAINT fk_line_batch FOREIGN KEY (batch_id) REFERENCES target_batches(id) ON DELETE CASCADE,
  CONSTRAINT fk_line_store FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE,
  CONSTRAINT fk_line_transfer FOREIGN KEY (transfer_id) REFERENCES store_region_transfers(id) ON DELETE SET NULL,
  INDEX ix_target_lines_region (region)
);
