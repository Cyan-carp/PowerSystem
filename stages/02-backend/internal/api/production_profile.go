package api

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"strings"
)

// The explicit V4 production profile is a separate domain from the S06/S08/S09
// local test hosts. It never falls back to a local guard when the profile is
// absent, wrong, or its machine identity changed.
const (
	v4ProductionProfile        = "v4-business-schedule/v1"
	v4BusinessProductionPolicy = "v4-s06-production/v1"
	v4ScheduleProductionPolicy = "v4-s08-production/v1"
)

// Fixed identity root; tests may substitute a local fixture directory.
var v4ProductionIdentityRoot = "/run/v4-production"

type v4ProductionHost struct {
	Hostname        string `json:"hostname"`
	VMID            int    `json:"vm_id"`
	MachineIDSHA256 string `json:"machine_id_sha256"`
	DMIUUID         string `json:"dmi_uuid"`
}

type v4ProductionIdentityDocument struct {
	Profile      string            `json:"profile"`
	Host         v4ProductionHost  `json:"host"`
	Databases    map[string]string `json:"databases"`
	Origins      map[string]string `json:"origins"`
	Capabilities map[string]bool   `json:"capabilities"`
	Policies     map[string]string `json:"policies"`
}

// loadV4ProductionIdentity mirrors the Python helper: exact schema, exact
// values, raw-byte machine-id digest, DMI product UUID and VM 117.
func loadV4ProductionIdentity() (v4ProductionIdentityDocument, error) {
	var doc v4ProductionIdentityDocument
	if os.Getenv("V4_PRODUCTION_PROFILE") != v4ProductionProfile {
		return doc, errors.New("explicit_v4_production_profile_required")
	}
	root := v4ProductionIdentityRoot
	raw, err := os.ReadFile(filepath.Join(root, "identity.json"))
	if err != nil {
		return doc, errors.New("invalid_v4_production_identity")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&doc) != nil || decoder.Decode(new(any)) != io.EOF {
		return doc, errors.New("invalid_v4_production_identity")
	}
	if doc.Profile != v4ProductionProfile ||
		!reflect.DeepEqual(doc.Databases, map[string]string{"business": "powersystem", "rag": "powersystem_agent_rag", "ops": "ma_ops"}) ||
		!reflect.DeepEqual(doc.Origins, map[string]string{"api": "http://api:8080", "rag": "http://agent-rag:8093", "ops": "http://ops-agent:8094", "security": "http://security-agent:8100"}) ||
		!reflect.DeepEqual(doc.Capabilities, map[string]bool{"actions": true, "schedules": true, "ops_write": false}) ||
		!reflect.DeepEqual(doc.Policies, map[string]string{"business": v4BusinessProductionPolicy, "schedule": v4ScheduleProductionPolicy}) {
		return doc, errors.New("invalid_v4_production_identity")
	}
	machine, err := os.ReadFile(filepath.Join(root, "machine-id"))
	if err != nil {
		return doc, errors.New("v4_production_host_identity_changed")
	}
	product, err := os.ReadFile(filepath.Join(root, "product-uuid"))
	if err != nil {
		return doc, errors.New("v4_production_host_identity_changed")
	}
	sum := sha256.Sum256(machine)
	if doc.Host.Hostname != "powersystem-prod" || doc.Host.VMID != 117 ||
		hex.EncodeToString(sum[:]) != doc.Host.MachineIDSHA256 ||
		strings.ToLower(strings.TrimSpace(string(product))) != doc.Host.DMIUUID {
		return doc, errors.New("v4_production_host_identity_changed")
	}
	return doc, nil
}

// v4ProductionConfig accepts only the DSN produced by Config.Load for the
// production business database, the two fixed internal origins, and a
// schedule bridge token distinct from every other service credential.
func (s *Server) v4ProductionConfig() error {
	if os.Getenv("V4_ACTIONS_ENABLED") != "true" || os.Getenv("V4_SCHEDULES_ENABLED") != "true" || os.Getenv("V4_OPS_WRITE_ENABLED") != "false" {
		return errors.New("invalid_v4_production_capability_matrix")
	}
	dsn, err := url.Parse(s.cfg.PostgresDSN)
	if err != nil || (dsn.Scheme != "postgres" && dsn.Scheme != "postgresql") ||
		dsn.Hostname() != "postgres" || dsn.Port() != "5432" || dsn.Path != "/powersystem" || dsn.Fragment != "" ||
		(dsn.RawQuery != "" && dsn.RawQuery != "sslmode=disable&timezone=UTC") {
		return errors.New("invalid_v4_production_business_datasource")
	}
	if s.cfg.AgentChatURL != "http://agent-rag:8093" || s.cfg.OpsURL != "http://ops-agent:8094" ||
		len(s.cfg.AgentToken) < 32 || len(s.cfg.OpsToken) < 32 || len(s.cfg.MAActorKey) < 32 {
		return errors.New("invalid_v4_production_business_origins")
	}
	if os.Getenv("V4_JOINT_PROFILE") != "" {
		return errors.New("v4_production_joint_profile_conflict")
	}
	tokenFile := os.Getenv("SCHEDULE_BRIDGE_TOKEN_FILE")
	if tokenFile == "" {
		return errors.New("v4_production_schedule_bridge_token_required")
	}
	raw, err := os.ReadFile(tokenFile)
	if err != nil {
		return errors.New("v4_production_schedule_bridge_token_required")
	}
	token := strings.TrimSpace(string(raw))
	if len(token) < 32 || token == s.cfg.AgentToken || token == s.cfg.OpsToken || token == s.cfg.MAActorKey {
		return errors.New("independent_v4_production_schedule_bridge_token_required")
	}
	return nil
}

// v4Production is the single explicit production gate for business actions and
// the schedule bridge. It rechecks the live business database name.
func (s *Server) v4Production() bool {
	if _, err := loadV4ProductionIdentity(); err != nil {
		return false
	}
	if s.v4ProductionConfig() != nil || s.db == nil {
		return false
	}
	var current string
	if s.db.Raw("SELECT current_database()").Scan(&current).Error != nil || current != "powersystem" {
		return false
	}
	return true
}
