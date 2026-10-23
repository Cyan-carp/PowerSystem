package config

import (
	"errors"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/spf13/viper"
)

type Config struct {
	HTTPAddr                string
	Broker                  string
	ClientID                string
	QueuePath               string
	PostgresDSN             string
	RedisAddr               string
	RedisPassword           string
	TDURL                   string
	TDUser                  string
	TDPassword              string
	TDDatabase              string
	JWTSecret               string
	AIURL                   string
	AIEnabled               bool
	AIPollSeconds           int
	WSAllowedOrigins        []string
	BusinessNotifyEnabled   bool
	BusinessNotifyURL       string
	BusinessNotifyToken     string
	AgentEnabled            bool
	AgentURL                string
	AgentChatURL            string
	AgentChatTimeoutSeconds int
	AgentChatInputChars     int
	AgentChatRate           int
	AgentChatConcurrency    int
	AgentToken              string
	AgentInterpretEnabled   bool
	OpsURL                  string
	OpsToken                string
	MAActorKey              string
	FleetPath               string
	SimulationAlarmMode     string
	AgentInterpretProtocol  string
}

var identifier = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

func Load(service string) (Config, error) {
	v := viper.New()
	v.AutomaticEnv()
	v.SetDefault("HTTP_ADDR", "127.0.0.1:8080")
	v.SetDefault("AGENT_CHAT_TIMEOUT_SECONDS", 125)
	v.SetDefault("AGENT_CHAT_INPUT_CHARS", 32000)
	v.SetDefault("AGENT_CHAT_RATE", 10)
	v.SetDefault("AGENT_CHAT_CONCURRENCY", 4)
	v.SetDefault("AI_URL", "http://127.0.0.1:8090")
	v.SetDefault("AI_ENABLED", false)
	v.SetDefault("AI_POLL_SECONDS", 300)
	v.SetDefault("MQTT_BROKER", "tcp://127.0.0.1:1883")
	v.SetDefault("MQTT_CLIENT_PREFIX", "powersystem-stage2")
	v.SetDefault("REDIS_ADDR", "127.0.0.1:6379")
	v.SetDefault("TDENGINE_URL", "http://127.0.0.1:6041")
	v.SetDefault("TDENGINE_DATABASE", "powersystem_stage2")
	v.SetDefault("TDENGINE_USER", "root")
	v.SetDefault("POSTGRES_HOST", "127.0.0.1")
	v.SetDefault("POSTGRES_PORT", "5432")
	v.SetDefault("POSTGRES_DB", "powersystem")
	v.SetDefault("POSTGRES_USER", "powersystem")
	v.SetDefault("STAGE2_DATA_DIR", filepath.Join("..", "..", "artifacts", "stage2", "runtime"))
	v.SetDefault("FLEET_CONFIG_PATH", filepath.Join("..", "01-data-chain", "simulator", "fleet.json"))
	v.SetDefault("SIMULATION_ALARM_MODE", "off")
	v.SetDefault("AGENT_INTERPRET_PROTOCOL", "v1")
	cfg := Config{
		HTTPAddr: v.GetString("HTTP_ADDR"), Broker: v.GetString("MQTT_BROKER"),
		ClientID:  v.GetString("MQTT_CLIENT_PREFIX") + "-" + service,
		QueuePath: filepath.Join(v.GetString("STAGE2_DATA_DIR"), service+".sqlite"),
		RedisAddr: v.GetString("REDIS_ADDR"), RedisPassword: v.GetString("REDIS_PASSWORD"),
		TDURL: v.GetString("TDENGINE_URL"), TDUser: v.GetString("TDENGINE_USER"),
		TDPassword: v.GetString("TDENGINE_ROOT_PASSWORD"), TDDatabase: v.GetString("TDENGINE_DATABASE"),
		JWTSecret: v.GetString("JWT_SECRET"), AIURL: v.GetString("AI_URL"), AIEnabled: v.GetBool("AI_ENABLED"), AIPollSeconds: v.GetInt("AI_POLL_SECONDS"),
		BusinessNotifyEnabled: v.GetBool("FEISHU_BUSINESS_ENABLED"), BusinessNotifyURL: v.GetString("FEISHU_BUSINESS_URL"),
		AgentEnabled: v.GetBool("AGENT_ENABLED"), AgentURL: v.GetString("AGENT_URL"),
		AgentChatURL: v.GetString("AGENT_CHAT_URL"), AgentChatTimeoutSeconds: v.GetInt("AGENT_CHAT_TIMEOUT_SECONDS"),
		AgentChatInputChars: v.GetInt("AGENT_CHAT_INPUT_CHARS"), AgentChatRate: v.GetInt("AGENT_CHAT_RATE"), AgentChatConcurrency: v.GetInt("AGENT_CHAT_CONCURRENCY"),
		AgentInterpretEnabled:  v.GetBool("AGENT_INTERPRET_ENABLED"),
		FleetPath:              v.GetString("FLEET_CONFIG_PATH"),
		SimulationAlarmMode:    v.GetString("SIMULATION_ALARM_MODE"),
		AgentInterpretProtocol: v.GetString("AGENT_INTERPRET_PROTOCOL"),
	}
	if cfg.SimulationAlarmMode != "off" && cfg.SimulationAlarmMode != "controlled" && cfg.SimulationAlarmMode != "paused" {
		return Config{}, errors.New("invalid SIMULATION_ALARM_MODE")
	}
	if cfg.AgentInterpretProtocol != "v1" && cfg.AgentInterpretProtocol != "v2" {
		return Config{}, errors.New("invalid AGENT_INTERPRET_PROTOCOL")
	}
	// Agent configuration must never prevent the business API from starting.
	if service == "api" {
		parsed, err := url.Parse(v.GetString("OPS_URL"))
		token, tErr := os.ReadFile(v.GetString("OPS_SERVICE_TOKEN_FILE"))
		key, kErr := os.ReadFile(v.GetString("MA_ACTOR_KEY_FILE"))
		if err == nil && parsed.Scheme == "http" && parsed.Host != "" && parsed.User == nil && parsed.Path == "" && parsed.RawQuery == "" && parsed.Fragment == "" && tErr == nil && kErr == nil && len(strings.TrimSpace(string(token))) >= 32 && len(strings.TrimSpace(string(key))) >= 32 {
			cfg.OpsURL = strings.TrimRight(v.GetString("OPS_URL"), "/")
			cfg.OpsToken = strings.TrimSpace(string(token))
			cfg.MAActorKey = strings.TrimSpace(string(key))
		}
	}
	if cfg.AgentEnabled && service == "api" {
		if cfg.AgentChatURL != "" {
			chatURL, err := url.Parse(cfg.AgentChatURL)
			if err != nil || chatURL.Scheme != "http" || chatURL.Host == "" || chatURL.User != nil || chatURL.RawQuery != "" || chatURL.Fragment != "" || (chatURL.Path != "" && chatURL.Path != "/") {
				cfg.AgentChatURL = ""
			} else {
				cfg.AgentChatURL = strings.TrimRight(cfg.AgentChatURL, "/")
			}
		}
		if cfg.AgentURL == "" {
			cfg.AgentURL = "http://127.0.0.1:8092"
		}
		parsed, parseErr := url.Parse(cfg.AgentURL)
		content, readErr := os.ReadFile(v.GetString("AGENT_SERVICE_TOKEN_FILE"))
		if parseErr == nil && parsed.Scheme == "http" && parsed.Host != "" && parsed.User == nil && (parsed.Path == "" || parsed.Path == "/") && parsed.RawQuery == "" && parsed.Fragment == "" && readErr == nil && len(strings.TrimSpace(string(content))) >= 32 {
			cfg.AgentToken = strings.TrimSpace(string(content))
			cfg.AgentURL = strings.TrimRight(cfg.AgentURL, "/")
		}
	}
	if cfg.BusinessNotifyEnabled && service == "api" {
		parsed, err := url.Parse(cfg.BusinessNotifyURL)
		if err != nil || parsed.Scheme != "http" || parsed.Host == "" || parsed.Path != "/business" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
			return Config{}, errors.New("FEISHU_BUSINESS_URL must be an internal HTTP /business endpoint")
		}
		content, err := os.ReadFile(v.GetString("FEISHU_BUSINESS_TOKEN_FILE"))
		if err != nil {
			return Config{}, errors.New("FEISHU_BUSINESS_TOKEN_FILE is required and unreadable")
		}
		cfg.BusinessNotifyToken = strings.TrimSpace(string(content))
		if len(cfg.BusinessNotifyToken) < 32 {
			return Config{}, errors.New("FEISHU_BUSINESS_TOKEN_FILE must contain a 32+ character token")
		}
	}
	for _, raw := range strings.Split(v.GetString("WS_ALLOWED_ORIGINS"), ",") {
		origin := strings.TrimSpace(raw)
		if origin == "" {
			continue
		}
		parsed, err := url.Parse(origin)
		if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.Path != "" || parsed.RawQuery != "" || parsed.Fragment != "" {
			return Config{}, errors.New("WS_ALLOWED_ORIGINS must contain exact http(s) origins")
		}
		cfg.WSAllowedOrigins = append(cfg.WSAllowedOrigins, origin)
	}
	u := &url.URL{Scheme: "postgres", User: url.UserPassword(v.GetString("POSTGRES_USER"), v.GetString("POSTGRES_PASSWORD")), Host: v.GetString("POSTGRES_HOST") + ":" + v.GetString("POSTGRES_PORT"), Path: "/" + v.GetString("POSTGRES_DB")}
	q := u.Query()
	q.Set("sslmode", "disable")
	q.Set("timezone", "UTC")
	u.RawQuery = q.Encode()
	cfg.PostgresDSN = u.String()
	if v.GetString("POSTGRES_PASSWORD") == "" || cfg.TDPassword == "" || cfg.RedisPassword == "" || len(cfg.JWTSecret) < 32 {
		return Config{}, errors.New("POSTGRES_PASSWORD, REDIS_PASSWORD, TDENGINE_ROOT_PASSWORD and JWT_SECRET (32+ characters) are required")
	}
	if cfg.AIPollSeconds < 5 || cfg.AIPollSeconds > 3600 {
		return Config{}, errors.New("AI_POLL_SECONDS must be 5..3600")
	}
	if !identifier.MatchString(cfg.TDDatabase) {
		return Config{}, errors.New("invalid TDENGINE_DATABASE")
	}
	return cfg, nil
}

const TokenTTL = 8 * time.Hour
