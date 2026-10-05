-- ============================================================
-- 「源网智联」数据库建表脚本
-- 对应笔记：架构设计/03-数据库设计
-- 分两段：① PostgreSQL 关系库  ② TDengine 时序库
-- 命名规范：小写下划线；每张关系表必有 id / created_at / updated_at
-- ============================================================

-- ============================================================
-- ① PostgreSQL 关系库（业务数据：用户 / 设备 / 告警 / 工单）
-- ============================================================

-- 用户表（FR-08 登录鉴权，密码 bcrypt 加密，不存明文）
CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    username      VARCHAR(64)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,                 -- bcrypt 哈希
    role          VARCHAR(32)  NOT NULL DEFAULT 'operator', -- operator / admin
    real_name     VARCHAR(64),
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- 设备表（FR-05 台账；device_code 与 MQTT 主题 / 时序库 tag 对齐）
CREATE TABLE IF NOT EXISTS devices (
    id          BIGSERIAL PRIMARY KEY,
    device_code VARCHAR(64)  NOT NULL UNIQUE,            -- 设备编号
    name        VARCHAR(128) NOT NULL,
    dev_type    VARCHAR(32)  NOT NULL,                   -- inverter / sensor ...
    vendor      VARCHAR(64),
    station_id  BIGINT,                                  -- 所属场站（本期单场站，预留）
    group_name  VARCHAR(64),
    status      VARCHAR(32)  NOT NULL DEFAULT 'offline', -- online / offline / fault
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_devices_group ON devices (group_name);

-- 告警规则表（FR-02/03 越限判断依据）
CREATE TABLE IF NOT EXISTS alarm_rules (
    id         BIGSERIAL PRIMARY KEY,
    device_id  BIGINT             NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    metric     VARCHAR(32)        NOT NULL,             -- temperature / voltage ...
    operator   VARCHAR(8)         NOT NULL,             -- > / < / >= / <= / ==
    threshold  DOUBLE PRECISION   NOT NULL,
    level      VARCHAR(16)        NOT NULL DEFAULT 'major', -- urgent / major / minor
    enabled    BOOLEAN            NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ        NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ        NOT NULL DEFAULT now()
);

-- 告警记录表（FR-02/04 告警闭环）
CREATE TABLE IF NOT EXISTS alarm_records (
    id           BIGSERIAL PRIMARY KEY,
    device_id    BIGINT       NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    rule_id      BIGINT       REFERENCES alarm_rules (id),
    metric       VARCHAR(32)  NOT NULL,
    level        VARCHAR(16)  NOT NULL,
    value        DOUBLE PRECISION,
    threshold    DOUBLE PRECISION,
    status       VARCHAR(16)  NOT NULL DEFAULT 'unhandled', -- unhandled / acked
    acked_by     BIGINT       REFERENCES users (id),
    acked_at     TIMESTAMPTZ,
    triggered_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alarm_device_status ON alarm_records (device_id, status);
CREATE INDEX IF NOT EXISTS idx_alarm_status_time   ON alarm_records (status, triggered_at DESC);

-- 工单表（FR-14 Could，预留）
CREATE TABLE IF NOT EXISTS work_orders (
    id         BIGSERIAL PRIMARY KEY,
    device_id  BIGINT       REFERENCES devices (id) ON DELETE SET NULL,
    alarm_id   BIGINT       REFERENCES alarm_records (id),
    title      VARCHAR(128) NOT NULL,
    status     VARCHAR(16)  NOT NULL DEFAULT 'open',   -- open / processing / done
    assignee   BIGINT       REFERENCES users (id),
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ============================================================
-- ② TDengine 时序库（遥测数据：写多读少、海量、带时间戳）
-- ============================================================

-- 1) 建库：TDengine 3 语法，毫秒精度，保留 365 天
CREATE DATABASE IF NOT EXISTS powersystem PRECISION 'ms' KEEP 365 DURATION 10;

-- 2) 超级表：设备遥测
--    tag   = 设备/场站编号（可索引，按设备快速查询）
--    field = 指标数值（不可索引）
USE powersystem;

CREATE STABLE IF NOT EXISTS telemetry (
    ts          TIMESTAMP,     -- 上报时间戳（每设备子表内为主键）
    seq         BIGINT,        -- 模拟器序号，供完整性对账
    voltage     FLOAT,         -- 电压 V
    current     FLOAT,         -- 电流 A
    temperature FLOAT,         -- 温度 ℃
    power       FLOAT,         -- 功率 kW
    status      INT,           -- 0 待机，1 运行，2 故障
    fault_code  INT            -- 独立故障码
) TAGS (
    device_id   BINARY(64),    -- 设备编号（对应 devices.device_code）
    station_id  BINARY(64)     -- 场站编号
);

-- 3) 每台设备一张子表（device_id 进 tag，实现按设备检索 + 索引）
CREATE TABLE IF NOT EXISTS t_inv_1001 USING telemetry TAGS ('INV-1001', 'ST-01');
CREATE TABLE IF NOT EXISTS t_inv_1002 USING telemetry TAGS ('INV-1002', 'ST-01');
CREATE TABLE IF NOT EXISTS t_inv_1003 USING telemetry TAGS ('INV-1003', 'ST-01');
