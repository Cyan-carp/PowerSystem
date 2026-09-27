CREATE DATABASE IF NOT EXISTS powersystem_stage2 PRECISION 'ms' KEEP 365 DURATION 10;
CREATE STABLE IF NOT EXISTS powersystem_stage2.telemetry (
    ts TIMESTAMP,
    seq BIGINT,
    voltage FLOAT,
    current FLOAT,
    temperature FLOAT,
    power FLOAT,
    status INT,
    fault_code INT
) TAGS (device_id BINARY(64), station_id BINARY(64));
