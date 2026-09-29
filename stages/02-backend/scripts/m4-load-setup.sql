INSERT INTO devices (device_code, name, dev_type, station_code)
SELECT 'LOAD-' || lpad(i::text, 4, '0'),
       'M4 synthetic device ' || i,
       'inverter',
       'ST-LOAD'
FROM generate_series(1, 200) AS i
ON CONFLICT (device_code) DO NOTHING;
