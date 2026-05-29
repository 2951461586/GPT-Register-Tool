package protocol

import "encoding/json"

type State map[string]any

func ParseState(raw string) (State, error) {
	if raw == "" {
		return State{}, nil
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil, err
	}
	if out == nil {
		out = map[string]any{}
	}
	return State(out), nil
}

func (s State) JSON() (string, error) {
	if s == nil {
		s = State{}
	}
	raw, err := CompactJSON(map[string]any(s))
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

func (s State) String(key string) string {
	if s == nil {
		return ""
	}
	return StringAt(map[string]any(s), key)
}

func (s State) With(key string, value any) State {
	if s == nil {
		s = State{}
	}
	s[key] = value
	return s
}
