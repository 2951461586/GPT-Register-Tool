package protocol

import "fmt"

type HTTPError struct {
	Operation  string
	Method     string
	URL        string
	StatusCode int
	Body       string
}

func (e *HTTPError) Error() string {
	op := e.Operation
	if op == "" {
		op = e.Method
	}
	if e.StatusCode == 0 {
		return fmt.Sprintf("%s %s failed: %s", op, e.URL, e.Body)
	}
	if e.Body == "" {
		return fmt.Sprintf("%s %s failed: status=%d", op, e.URL, e.StatusCode)
	}
	return fmt.Sprintf("%s %s failed: status=%d body=%s", op, e.URL, e.StatusCode, e.Body)
}

type ConfigError struct {
	Field string
	Msg   string
}

func (e *ConfigError) Error() string {
	if e.Field == "" {
		return e.Msg
	}
	return e.Field + ": " + e.Msg
}
