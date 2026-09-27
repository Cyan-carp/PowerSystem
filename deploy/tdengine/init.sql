-- Replace {{DATABASE}} with an identifier containing lowercase letters, digits, or underscore.
CREATE DATABASE IF NOT EXISTS {{DATABASE}} PRECISION 'ms' KEEP 365 DURATION 10;
CREATE STABLE IF NOT EXISTS {{DATABASE}}.telemetry (
    ts TIMESTAMP,
    seq BIGINT,
    voltage FLOAT,
    current FLOAT,
    temperature FLOAT,
    power FLOAT,
    status INT,
    fault_code INT
) TAGS (device_id BINARY(64), station_id BINARY(64));
CREATE TABLE IF NOT EXISTS {{DATABASE}}.t_inv_1001 USING {{DATABASE}}.telemetry TAGS ('INV-1001', 'ST-01');
CREATE TABLE IF NOT EXISTS {{DATABASE}}.t_inv_1002 USING {{DATABASE}}.telemetry TAGS ('INV-1002', 'ST-01');
CREATE TABLE IF NOT EXISTS {{DATABASE}}.t_inv_1003 USING {{DATABASE}}.telemetry TAGS ('INV-1003', 'ST-01');

