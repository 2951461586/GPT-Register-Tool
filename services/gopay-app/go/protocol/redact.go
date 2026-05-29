package protocol

import (
	"regexp"
	"strings"
)

var (
	bearerTokenRe = regexp.MustCompile(`(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}`)
	keyValueRe    = regexp.MustCompile(`(?i)\b(access_token|refresh_token|session_token|csrf_token|token|pin|otp|password|secret|cookie)\s*[:=]\s*['"]?[^'"\s,}]{6,}`)
	jwtRe         = regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b`)
	longTokenRe   = regexp.MustCompile(`\b[A-Za-z0-9_-]{48,}\b`)
	urlRe         = regexp.MustCompile(`(?i)\b(?:https?|gopay)://[^\s"'<>]+`)
)

func RedactText(value string) string {
	out := urlRe.ReplaceAllString(value, "<redacted-url>")
	out = bearerTokenRe.ReplaceAllString(out, "Bearer <redacted>")
	out = keyValueRe.ReplaceAllString(out, "$1=<redacted>")
	out = jwtRe.ReplaceAllString(out, "<redacted-token>")
	out = longTokenRe.ReplaceAllString(out, "<redacted-token>")
	return out
}

func Snippet(value string, limit int) string {
	value = strings.TrimSpace(value)
	if limit <= 0 || len(value) <= limit {
		return value
	}
	return value[:limit]
}
