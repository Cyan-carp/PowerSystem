-- Rule-driven alarms for the verified synthetic fleet.
-- Never replace an operator's pre-existing rule on the same device and metric.
-- The 17:00 power event is an upper bound because a lower bound would fire
-- normally every night. The selected sample must exceed 110 kW.
INSERT INTO alarm_rules(device_id, metric, operator, threshold, level, enabled, version)
SELECT p.device_id, r.metric, r.operator, r.threshold, r.level, TRUE, 'fleet-natural-v1'
FROM simulation_alarm_policy_devices AS p
JOIN devices AS d ON d.id = p.device_id
JOIN (VALUES
 ('PV-01', 'temperature', '>', 55.0, 'minor'),
 ('PV-01', 'voltage', '>', 430.0, 'major'),
 ('PV-01', 'current', '>', 180.0, 'major'),
 ('PV-01', 'power', '>', 110.0, 'minor'),
 ('PV-02', 'temperature', '>', 75.0, 'major'),
 ('PV-02', 'voltage', '>', 430.0, 'major'),
 ('PV-02', 'current', '>', 180.0, 'major'),
 ('PV-02', 'power', '>', 110.0, 'minor'),
 ('PV-03', 'temperature', '>', 80.0, 'urgent'),
 ('PV-03', 'voltage', '>', 430.0, 'major'),
 ('PV-03', 'current', '>', 180.0, 'major'),
 ('PV-03', 'power', '>', 110.0, 'minor'),
 ('PV-04', 'temperature', '>', 55.0, 'minor'),
 ('PV-04', 'voltage', '>', 430.0, 'major'),
 ('PV-04', 'current', '>', 180.0, 'major'),
 ('PV-04', 'power', '>', 110.0, 'minor'),
 ('ST-01', 'temperature', '>', 80.0, 'urgent'),
 ('ST-01', 'voltage', '>', 430.0, 'major'),
 ('ST-01', 'current', '>', 180.0, 'major'),
 ('ST-01', 'power', '>', 110.0, 'minor')
) AS r(station_code, metric, operator, threshold, level)
  ON r.station_code = p.station_code
WHERE p.active = TRUE AND d.deleted_at IS NULL
ON CONFLICT(device_id, metric) DO NOTHING;
