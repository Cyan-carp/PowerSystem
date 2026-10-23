-- 009_simulation_alarms.sql
-- 受控演示告警：日程槽位、经清单核对后的持久设备身份、告警归因列。
-- 演示只写告警/通知事实，不写遥测和预测；metric=simulation_scenario,
-- value=1, threshold=0，与物理量和预测概率保持可区分。
-- 兼容：origin 带默认值 normal，旧记录自动归为自然告警；场景列为空。
-- 执行：由 cmd/api/main.go 在 008 之后顺序追加，失败停止。

ALTER TABLE alarm_records ADD COLUMN IF NOT EXISTS origin VARCHAR(16) NOT NULL DEFAULT 'normal';
ALTER TABLE alarm_records ADD COLUMN IF NOT EXISTS scenario_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_alarm_records_origin_triggered
    ON alarm_records(origin, triggered_at DESC);

-- 持久受控身份：只保存 fleetCatalog 中按 station_code+device_code 核对过台账的设备。
-- 自然告警门控只读本表，不依赖 devices.is_simulated（ST-01 三台可能为 FALSE）。
-- 核对失败时不删除、不停用既有行，避免受控设备退回自然告警。
CREATE TABLE IF NOT EXISTS simulation_alarm_policy_devices (
    id BIGSERIAL PRIMARY KEY,
    station_code VARCHAR(64) NOT NULL,
    device_code VARCHAR(64) NOT NULL,
    device_id BIGINT NOT NULL REFERENCES devices(id),
    source VARCHAR(32) NOT NULL DEFAULT 'fleet_catalog',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(station_code, device_code)
);

CREATE INDEX IF NOT EXISTS idx_simulation_policy_device
    ON simulation_alarm_policy_devices(device_id)
    WHERE active = TRUE;

-- 单例状态：首次启用受控模式的时间。worker 在第一次进入 controlled 时写入，
-- 之后永不覆盖；日期早于该时刻的槽位只记 skipped，不补发。
CREATE TABLE IF NOT EXISTS simulation_alarm_state (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    first_enabled_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 每日五个槽位：09/11/13/15/17 Asia/Shanghai，依次对应 PV-01/02/03/04/ST-01。
-- 唯一键只有 (slot_date, slot_hour)，不含策略版本；同一天重复启动不新增、不洗牌。
-- status: pending/fired/skipped/ended
-- reason: before_first_enable/window_missed/simulation_demo_started/
--         simulation_demo_ended/无可用的最新遥测等跳过原因
CREATE TABLE IF NOT EXISTS simulation_alarm_slots (
    id BIGSERIAL PRIMARY KEY,
    slot_date DATE NOT NULL,
    slot_hour SMALLINT NOT NULL,
    station_code VARCHAR(64) NOT NULL,
    device_id BIGINT NOT NULL REFERENCES devices(id),
    device_code VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    reason VARCHAR(64),
    alarm_id BIGINT REFERENCES alarm_records(id) ON DELETE SET NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(slot_date, slot_hour)
);

CREATE INDEX IF NOT EXISTS idx_simulation_slots_due
    ON simulation_alarm_slots(status, scheduled_at);

CREATE INDEX IF NOT EXISTS idx_simulation_slots_device
    ON simulation_alarm_slots(slot_date, device_id);

-- scenario_id 指向 simulation_alarm_slots.id。这里不建循环外键，
-- 只有受控演示写入该列，取证时 join slots 可取得日期/时段/场站/设备。
COMMENT ON COLUMN alarm_records.origin IS '告警来源：normal 自然告警，simulation 受控演示';
COMMENT ON COLUMN alarm_records.scenario_id IS '受控演示槽位 simulation_alarm_slots.id，自然告警为 NULL';
COMMENT ON TABLE simulation_alarm_policy_devices IS '经 fleetCatalog 核对台账后的受控设备持久身份，供自然告警新建门控使用';
COMMENT ON COLUMN simulation_alarm_policy_devices.active IS 'true 表示该设备仍受自然告警新建抑制；核对异常时不自动停用';
COMMENT ON TABLE simulation_alarm_state IS '受控演示单例状态，first_enabled_at 为首次启用时刻';
COMMENT ON TABLE simulation_alarm_slots IS '每日五次受控演示日程及取证元数据';
COMMENT ON COLUMN simulation_alarm_slots.status IS 'pending 待执行，fired 已触发，skipped 跳过，ended 演示到期结束';
COMMENT ON COLUMN simulation_alarm_slots.reason IS '槽位结果原因，含窗口错过、首次启用前、演示开始/结束及可用性跳过原因';
COMMENT ON COLUMN simulation_alarm_slots.started_at IS '演示告警实际触发时间，结束时间由它加十分钟计算';
COMMENT ON COLUMN simulation_alarm_slots.ended_at IS '演示到期结束时间';
