package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
)

type JSONMap map[string]any

func CompactJSON(value any) ([]byte, error) {
	if value == nil {
		return nil, nil
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSpace(buf.Bytes()), nil
}

func DecodeJSONMap(raw []byte) (JSONMap, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return JSONMap{}, nil
	}
	var payload any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	if obj, ok := payload.(map[string]any); ok {
		return obj, nil
	}
	return JSONMap{"value": payload}, nil
}

func DataObject(payload JSONMap) JSONMap {
	if payload == nil {
		return JSONMap{}
	}
	if data, ok := objectAt(payload["data"]); ok && data != nil {
		return data
	}
	return payload
}

func StringAt(value any, path ...string) string {
	current := value
	for _, key := range path {
		obj, ok := objectAt(current)
		if !ok {
			return ""
		}
		current = obj[key]
	}
	switch typed := current.(type) {
	case string:
		return typed
	case fmt.Stringer:
		return typed.String()
	case nil:
		return ""
	default:
		return fmt.Sprint(typed)
	}
}

func objectAt(value any) (map[string]any, bool) {
	switch typed := value.(type) {
	case map[string]any:
		return typed, typed != nil
	case JSONMap:
		return map[string]any(typed), typed != nil
	default:
		return nil, false
	}
}

func IntAt(value any, path ...string) int64 {
	text := StringAt(value, path...)
	if text == "" {
		return 0
	}
	parsed, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return 0
	}
	return int64(parsed)
}
