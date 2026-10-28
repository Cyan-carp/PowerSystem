package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
	"powersystem/backend/internal/model"
	"powersystem/backend/internal/service"
	"powersystem/backend/internal/telemetry"
)

const snapshotPageSize = 128
const snapshotParallelism = 8
const snapshotCacheTTL = 10 * time.Minute

// Decimal strings preserve all BIGINT bits. Lua numbers cannot represent every
// ID above 2^53. Neither run_id nor seq is an ordering clock.
var snapshotCAS = redis.NewScript(`
 local old=redis.call('GET',KEYS[1])
 if old then
  local ok,v=pcall(cjson.decode,old)
  if ok and type(v.receipt_order)=='string' and v.receipt_order>=ARGV[1] then
   if ARGV[3]~='1' then return 0 end
   if old==ARGV[2] then
    local ttl=redis.call('PTTL',KEYS[1])
    if ttl<0 or ttl>tonumber(ARGV[4]) then redis.call('PEXPIRE',KEYS[1],ARGV[4]); return 2 end
    return 0
   end
  end
 end
 redis.call('SET',KEYS[1],ARGV[2],'PX',ARGV[4]); return 1
`)

func snapshotValue(d model.Device, item model.Inbox, sample telemetry.Sample) gin.H {
	return gin.H{"snapshot_source": "td_confirmed", "business_commit": "not_asserted", "receipt_order": fmt.Sprintf("%020d", item.ID), "inbox_id": fmt.Sprint(item.ID), "device_id": d.ID, "device_code": d.DeviceCode, "run_id": sample.RunID, "seq": sample.Seq, "ts_ms": sample.TS, "received_at": item.ReceivedAt.UTC(), "voltage": sample.Voltage, "current": sample.Current, "temperature": sample.Temperature, "power": sample.Power, "status": sample.Status, "fault_code": sample.FaultCode}
}
func (s *Server) writeSnapshot(ctx context.Context, d model.Device, item model.Inbox, sample telemetry.Sample, durableSource ...bool) (gin.H, error) {
	if item.ID < 1 || item.TDConfirmedAt == nil {
		return nil, errors.New("snapshot_unconfirmed")
	}
	latest := snapshotValue(d, item, sample)
	b, err := json.Marshal(latest)
	if err != nil {
		return nil, err
	}
	dep, cancel := context.WithTimeout(ctx, telemetryDependencyBudget)
	defer cancel()
	force := "0"
	if len(durableSource) > 0 && durableSource[0] {
		force = "1"
	}
	changed, err := snapshotCAS.Run(dep, s.redis, []string{fmt.Sprintf("device:%d:latest", d.ID)}, latest["receipt_order"], b, force, snapshotCacheTTL.Milliseconds()).Int()
	if err != nil {
		return nil, err
	}
	if changed == 1 {
		s.telemetry.touch("redis_write", time.Now())
		s.broadcast(gin.H{"type": "telemetry", "data": latest})
	} else if changed == 2 {
		s.telemetry.touch("redis_write", time.Now())
	} else {
		s.telemetry.touch("snapshot_unchanged", time.Now())
	}
	return latest, nil
}

func (s *Server) snapshotWorker(ctx context.Context) {
	timer := time.NewTicker(500 * time.Millisecond)
	defer timer.Stop()
	for {
		if err := s.snapshotSweep(ctx); err != nil && ctx.Err() == nil {
			s.telemetryLog("snapshot_sweep", time.Now())
		}
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
	}
}

// Each page holds bounded registry/candidate memory; the cursor is durable.
// Repeated publication repairs Redis after a restart, including silent devices.
func (s *Server) snapshotSweep(parent context.Context) error {
	s.snapshotMu.Lock()
	defer s.snapshotMu.Unlock()
	ctx, cancel := context.WithTimeout(parent, inboxTickBudget)
	defer cancel()
	var cursor struct{ RealtimeLastDeviceID int64 }
	r := s.db.WithContext(ctx).Raw("SELECT realtime_last_device_id FROM telemetry_scheduler WHERE id=1").Scan(&cursor)
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected != 1 {
		return errors.New("scheduler_missing")
	}
	var devices []model.Device
	if err := s.db.WithContext(ctx).Where("deleted_at IS NULL AND id>?", cursor.RealtimeLastDeviceID).Order("id").Limit(snapshotPageSize).Find(&devices).Error; err != nil {
		return err
	}
	if len(devices) < snapshotPageSize && cursor.RealtimeLastDeviceID > 0 {
		var tail []model.Device
		if e := s.db.WithContext(ctx).Where("deleted_at IS NULL AND id<=?", cursor.RealtimeLastDeviceID).Order("id").Limit(snapshotPageSize - len(devices)).Find(&tail).Error; e != nil {
			return e
		}
		devices = append(devices, tail...)
	}
	if len(devices) == 0 {
		return s.advanceTelemetryCursor(ctx, "realtime_last_device_id", 0)
	}

	var first error
	var mu sync.Mutex
	jobs := make(chan model.Device, snapshotParallelism)
	var workers sync.WaitGroup
	for n := 0; n < snapshotParallelism; n++ {
		workers.Add(1)
		go func() {
			defer workers.Done()
			for d := range jobs {
				if ctx.Err() == nil {
					if e := s.refreshDeviceSnapshot(ctx, d); e != nil {
						mu.Lock()
						if first == nil {
							first = e
						}
						mu.Unlock()
					}
				}
			}
		}()
	}
	for begin := 0; begin < len(devices); begin += snapshotParallelism {
		if ctx.Err() != nil {
			break
		}
		group := devices[begin:min(begin+snapshotParallelism, len(devices))]
		if e := s.advanceTelemetryCursor(ctx, "realtime_last_device_id", group[len(group)-1].ID); e != nil {
			mu.Lock()
			if first == nil {
				first = e
			}
			mu.Unlock()
			break
		}
		for _, d := range group {
			select {
			case jobs <- d:
			case <-ctx.Done():
			}
		}
	}
	close(jobs)
	workers.Wait()
	return first
}

func validSnapshotItem(d model.Device, i model.Inbox) (telemetry.Sample, bool) {
	p, e := telemetry.ParseStored(i.Payload)
	if e != nil || p.RunID != i.RunID || p.Seq != i.Seq || p.TS != i.TS || p.DeviceCode != d.DeviceCode || p.StationCode != d.StationCode {
		return p, false
	}
	// Historical backfill cannot advance the real-time high water mark. A device
	// clock correction may catch up, but the existing timestamp is never rewound.
	return p, telemetry.Freshness(i.ReceivedAt, i.ReceivedAt, i.TS) == "fresh"
}

func (s *Server) refreshDeviceSnapshot(parent context.Context, d model.Device) error {
	measured := time.Now()
	defer func() { s.telemetry.elapsed("snapshot", time.Since(measured)) }()
	ctx, cancel := context.WithTimeout(parent, inboxItemBudget)
	defer cancel()
	confirm, endConfirm := context.WithTimeout(ctx, inboxTransactionBudget)
	defer endConfirm()
	// Reads/TD checks do not mutate either database. The later single-statement
	// commit rechecks identity and holds both maintenance/device locks until commit.
	if s.alarmMaintenance(confirm) {
		return service.ErrAlarmMaintenance
	}
	var items []model.Inbox
	if e := s.db.WithContext(confirm).Raw(`SELECT * FROM telemetry_inbox WHERE device_id=? AND id>COALESCE((SELECT inbox_id FROM telemetry_snapshots WHERE device_id=?),0) ORDER BY id DESC LIMIT 3`, d.ID, d.ID).Scan(&items).Error; e != nil {
		return e
	}
	var confirmationError error
	for _, i := range items {
		if _, valid := validSnapshotItem(d, i); !valid || i.BusinessState != nil && *i.BusinessState == "quarantined" {
			continue
		}
		if i.TDConfirmedAt == nil {
			if !s.snapshotTDCircuit.enter(time.Now()) {
				confirmationError = errDependencyOpen
				break
			}
			dep, end := context.WithTimeout(confirm, telemetryDependencyBudget)
			found, e := s.td.HasSample(dep, d.ID, i.TS, i.Seq)
			end()
			s.snapshotTDCircuit.finish(time.Now(), e, confirm.Err() != nil)
			if e != nil {
				confirmationError = e
				break
			}
			if !found {
				continue
			}
		}
		var accepted struct{ ID int64 }
		r := s.db.WithContext(confirm).Raw(`WITH gate AS MATERIALIZED(SELECT enabled FROM alarm_maintenance_state WHERE id=1 FOR SHARE),
  locked AS MATERIALIZED(SELECT pg_advisory_xact_lock(hashtextextended(?,0)) FROM gate WHERE NOT enabled),
  confirmed AS(UPDATE telemetry_inbox i SET td_confirmed_at=COALESCE(td_confirmed_at,clock_timestamp()),
   next_attempt_at=CASE WHEN business_state='retry' AND failure_code='td_missing' THEN clock_timestamp() ELSE next_attempt_at END
   FROM locked,devices d WHERE i.id=? AND i.device_id=? AND i.run_id=? AND i.seq=? AND i.ts_ms=? AND i.payload=?::jsonb
    AND (i.business_state IS NULL OR i.business_state<>'quarantined') AND d.id=i.device_id AND d.deleted_at IS NULL AND d.device_code=? AND d.station_code=? RETURNING i.id,i.device_id,i.ts_ms),
  source AS(INSERT INTO telemetry_snapshots(device_id,inbox_id,ts_ms,confirmed_at) SELECT device_id,id,ts_ms,clock_timestamp() FROM confirmed
   ON CONFLICT(device_id) DO UPDATE SET inbox_id=excluded.inbox_id,ts_ms=excluded.ts_ms,confirmed_at=excluded.confirmed_at
   WHERE telemetry_snapshots.inbox_id<excluded.inbox_id AND telemetry_snapshots.ts_ms<=excluded.ts_ms RETURNING inbox_id)
  SELECT id FROM confirmed`, fmt.Sprintf("telemetry-device:%d", d.ID), i.ID, d.ID, i.RunID, i.Seq, i.TS, string(i.Payload), d.DeviceCode, d.StationCode).Scan(&accepted)
		if r.Error != nil {
			return r.Error
		}
		if accepted.ID == 0 {
			return service.ErrAlarmMaintenance
		}
		break
	}
	endConfirm()
	publish, endPublish := context.WithTimeout(ctx, inboxTransactionBudget)
	defer endPublish()
	// Fast read only comparison avoids reacquiring/writing the hot maintenance row
	// when Redis already contains exactly the durable source. No write is admitted
	// by this read. A missing/mismatched cache still takes the real gate below.
	var source model.Inbox
	result := s.db.WithContext(publish).Raw(`SELECT i.* FROM telemetry_snapshots s JOIN telemetry_inbox i ON i.id=s.inbox_id WHERE s.device_id=? AND i.device_id=? AND i.td_confirmed_at IS NOT NULL`, d.ID, d.ID).Scan(&source)
	if result.Error != nil {
		return result.Error
	}
	dep, end := context.WithTimeout(publish, telemetryDependencyBudget)
	raw, cacheErr := s.redis.Get(dep, fmt.Sprintf("device:%d:latest", d.ID)).Bytes()
	end()
	if cacheErr == nil && source.ID > 0 {
		sample, valid := validSnapshotItem(d, source)
		expected, e := json.Marshal(snapshotValue(d, source, sample))
		if valid && e == nil && string(raw) == string(expected) {
			dep, end := context.WithTimeout(publish, telemetryDependencyBudget)
			ttl, e := s.redis.PTTL(dep, fmt.Sprintf("device:%d:latest", d.ID)).Result()
			end()
			if e == nil && ttl > 0 && ttl <= snapshotCacheTTL {
				return confirmationError
			}
		}
	}
	if !s.redisCircuit.enter(time.Now()) {
		return errDependencyOpen
	}
	err := service.TelemetryTransaction(s.db.WithContext(publish), func(tx *gorm.DB) error {
		if e := service.TelemetryDeviceLock(tx, d.ID); e != nil {
			return e
		}
		var current model.Inbox
		r := tx.Raw(`SELECT i.* FROM telemetry_snapshots s JOIN telemetry_inbox i ON i.id=s.inbox_id WHERE s.device_id=? AND i.device_id=? AND i.td_confirmed_at IS NOT NULL`, d.ID, d.ID).Scan(&current)
		if r.Error != nil {
			return r.Error
		}
		if r.RowsAffected == 0 {
			dep, end := context.WithTimeout(publish, telemetryDependencyBudget)
			defer end()
			return s.redis.Del(dep, fmt.Sprintf("device:%d:latest", d.ID)).Err()
		}
		sample, valid := validSnapshotItem(d, current)
		if !valid {
			return errors.New("snapshot_source_invalid")
		}
		_, e := s.writeSnapshot(publish, d, current, sample, true)
		return e
	})
	s.redisCircuit.finish(time.Now(), err, publish.Err() != nil)
	if err != nil {
		return err
	}
	return confirmationError
}

func (s *Server) restoreSnapshots(parent context.Context) error {
	ctx, cancel := context.WithTimeout(parent, restoreBudget)
	defer cancel()
	// Legacy success candidates are reverified by refreshDeviceSnapshot. No DEL
	// races with a newer publisher, and unknown is kept unknown, not fabricated.
	after := int64(0)
	for {
		var ds []model.Device
		if e := s.db.WithContext(ctx).Where("deleted_at IS NULL AND id>?", after).Order("id").Limit(snapshotPageSize).Find(&ds).Error; e != nil {
			return e
		}
		if len(ds) == 0 {
			return nil
		}
		sem := make(chan struct{}, snapshotParallelism)
		var wg sync.WaitGroup
		var first error
		var mu sync.Mutex
		for _, d := range ds {
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				wg.Wait()
				return ctx.Err()
			}
			wg.Add(1)
			go func(d model.Device) {
				defer wg.Done()
				defer func() { <-sem }()
				if e := s.refreshDeviceSnapshot(ctx, d); e != nil {
					mu.Lock()
					if first == nil {
						first = e
					}
					mu.Unlock()
				}
			}(d)
		}
		wg.Wait()
		if first != nil {
			return first
		}
		after = ds[len(ds)-1].ID
	}
}
