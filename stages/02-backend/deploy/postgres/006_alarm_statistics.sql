CREATE INDEX IF NOT EXISTS idx_alarm_records_triggered_device ON alarm_records(triggered_at, device_id);
CREATE INDEX IF NOT EXISTS idx_alarm_records_device_triggered ON alarm_records(device_id, triggered_at);
