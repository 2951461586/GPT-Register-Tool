package appsvc

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
)

type StateStore struct {
	dsn    string
	table  string
	ready  bool
	memory bool
	values map[string]string
	mu     sync.Mutex
}

func NewStateStore(dsn string, table string) (*StateStore, error) {
	dsn = strings.TrimSpace(dsn)
	if dsn == "" {
		return &StateStore{memory: true, values: map[string]string{}}, nil
	}
	table = strings.TrimSpace(table)
	if table == "" {
		table = "gopay_app_states"
	}
	if !regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`).MatchString(table) {
		return nil, fmt.Errorf("invalid GOPAY_STATE_TABLE: %s", table)
	}
	return &StateStore{dsn: dsn, table: table}, nil
}

func NormalizeStateKey(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" || value == "local" {
		return "local", nil
	}
	if strings.HasPrefix(value, "tg:") {
		userID := strings.TrimSpace(strings.TrimPrefix(value, "tg:"))
		if userID != "" && regexp.MustCompile(`^\d+$`).MatchString(userID) {
			return "tg:" + userID, nil
		}
	}
	return "", fmt.Errorf("user_id must be local or tg:<user_id>")
}

func (s *StateStore) Load(ctx context.Context, key string) (string, error) {
	if s.memory {
		s.mu.Lock()
		defer s.mu.Unlock()
		value := strings.TrimSpace(s.values[key])
		if value == "" {
			return "{}", nil
		}
		return value, nil
	}
	if err := s.ensure(ctx); err != nil {
		return "", err
	}
	conn, err := pgx.Connect(ctx, s.dsn)
	if err != nil {
		return "", err
	}
	defer conn.Close(ctx)
	var stateJSON string
	err = conn.QueryRow(ctx, fmt.Sprintf("SELECT state_json::text FROM %s WHERE state_key=$1", s.table), key).Scan(&stateJSON)
	if err == pgx.ErrNoRows {
		return "{}", nil
	}
	if err != nil {
		return "", err
	}
	if strings.TrimSpace(stateJSON) == "" {
		return "{}", nil
	}
	return stateJSON, nil
}

func (s *StateStore) Save(ctx context.Context, key string, raw string) (string, error) {
	state, err := parseState(raw)
	if err != nil {
		return "", err
	}
	normalized := stateJSON(state)
	if s.memory {
		s.mu.Lock()
		defer s.mu.Unlock()
		s.values[key] = normalized
		return normalized, nil
	}
	if err := s.ensure(ctx); err != nil {
		return "", err
	}
	conn, err := pgx.Connect(ctx, s.dsn)
	if err != nil {
		return "", err
	}
	defer conn.Close(ctx)
	now := time.Now().Unix()
	_, err = conn.Exec(ctx, fmt.Sprintf(`
INSERT INTO %s (state_key, state_json, created_at, updated_at)
VALUES ($1, $2::jsonb, $3, $4)
ON CONFLICT (state_key) DO UPDATE
SET state_json=EXCLUDED.state_json,
    updated_at=EXCLUDED.updated_at`, s.table), key, normalized, now, now)
	if err != nil {
		return "", err
	}
	return normalized, nil
}

func (s *StateStore) Delete(ctx context.Context, key string) error {
	if s.memory {
		s.mu.Lock()
		defer s.mu.Unlock()
		delete(s.values, key)
		return nil
	}
	if err := s.ensure(ctx); err != nil {
		return err
	}
	conn, err := pgx.Connect(ctx, s.dsn)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	_, err = conn.Exec(ctx, fmt.Sprintf("DELETE FROM %s WHERE state_key=$1", s.table), key)
	return err
}

func (s *StateStore) ensure(ctx context.Context) error {
	if s.memory {
		return nil
	}
	if s.ready {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ready {
		return nil
	}
	conn, err := pgx.Connect(ctx, s.dsn)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	_, err = conn.Exec(ctx, fmt.Sprintf(`
CREATE TABLE IF NOT EXISTS %s (
  state_key TEXT PRIMARY KEY,
  state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at BIGINT NOT NULL,
  updated_at BIGINT NOT NULL
)`, s.table))
	if err != nil {
		return err
	}
	s.ready = true
	return nil
}
