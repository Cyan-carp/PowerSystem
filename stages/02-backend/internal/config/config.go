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
	HTTPAddr              string
	Broker                string
	ClientID              string
	QueuePath             string
	PostgresDSN           string
	RedisAddr             string
	RedisPassword         string
	TDURL                 string
	TDUser                string
	TDPassword            string
	TDDatabase            string
	JWTSecret             string
	AIURL                 string
	AIEnabled             bool
	AIPollSeconds         int
	WSAllowedOrigins      []string
	BusinessNotifyEnabled bool
	BusinessNotifyURL     string
	BusinessNotifyToken   string
	AgentEnabled          bool
	AgentURL              string
	AgentToken            string
	AgentInterpretEnabled bool
}

var identifier = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)

func Load(service string) (Config, error) {
	v := viper.New()
	v.AutomaticEnv()
	v.SetDefault("HTTP_ADDR", "127.0.0.1:8080")
	v.SetDefault("AI_URL", "http://127.0.0.1:8090")
	v.SetDefault("AI_ENABLED", false)
	v.SetDefault("AI_POLL_SECONDS", 300)
	v.SetDefault("MQTT_BROKER", "tcp://127.0.0.1:1883")
	v.SetDefault("REDIS_ADDR", "127.0.0.1:6379")
	v.SetDefault("TDENGINE_URL", "http://127.0.0.1:6041")
	v.SetDefault("TDENGINE_DATABASE", "powersystem_stage2")
	v.SetDefault("TDENGINE_USER", "root")
	v.SetDefault("POSTGRES_HOST", "127.0.0.1")
	v.SetDefault("POSTGRES_PORT", "5432")
	v.SetDefault("POSTGRES_DB", "powersystem")
	v.SetDefault("POSTGRES_USER", "powersystem")
	v.SetDefault("STAGE2_DATA_DIR", filepath.Join("..", "..", "artifacts", "stage2", "runtime"))
	cfg := Config{
		HTTPAddr: v.GetString("HTTP_ADDR"), Broker: v.GetString("MQTT_BROKER"),
		ClientID:  "powersystem-stage2-" + service,
		QueuePath: filepath.Join(v.GetString("STAGE2_DATA_DIR"), service+".sqlite"),
		RedisAddr: v.GetString("REDIS_ADDR"), RedisPassword: v.GetString("REDIS_PASSWORD"),
		TDURL: v.GetString("TDENGINE_URL"), TDUser: v.GetString("TDENGINE_USER"),
		TDPassword: v.GetString("TDENGINE_ROOT_PASSWORD"), TDDatabase: v.GetString("TDENGINE_DATABASE"),
		JWTSecret: v.GetString("JWT_SECRET"), AIURL: v.GetString("AI_URL"), AIEnabled: v.GetBool("AI_ENABLED"), AIPollSeconds: v.GetInt("AI_POLL_SECONDS"),
		BusinessNotifyEnabled: v.GetBool("FEISHU_BUSINESS_ENABLED"), BusinessNotifyURL: v.GetString("FEISHU_BUSINESS_URL"),
		AgentEnabled: v.GetBool("AGENT_ENABLED"), AgentURL: v.GetString("AGENT_URL"),
		AgentInterpretEnabled: v.GetBool("AGENT_INTERPRET_ENABLED"),
	}
	// Agent configuration must never prevent the business API from starting.
	if cfg.AgentEnabled && service == "api" {
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
