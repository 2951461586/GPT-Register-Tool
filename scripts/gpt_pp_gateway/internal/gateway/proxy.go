package gateway

import (
	"encoding/json"
	"fmt"
	"net/url"
	"regexp"
	"strings"
	"time"
)

var tokenRE = regexp.MustCompile(`^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$`)
var tokenFindRE = regexp.MustCompile(`eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`)

func ExtractAccessToken(raw string) (string, error) {
	token := strings.TrimSpace(raw)
	if token == "" {
		return "", &APIError{Code: "missing_credential", Message: "请填写 accessToken 或 /api/auth/session JSON", Status: 400}
	}
	if strings.HasPrefix(token, "{") || strings.HasPrefix(token, "[") {
		if found := accessTokenFromJSON(token); found != "" {
			token = found
		} else {
			return "", &APIError{Code: "invalid_session_json", Message: "Session JSON 未找到 accessToken", Status: 400}
		}
	} else if !tokenRE.MatchString(token) {
		if found := tokenFindRE.FindString(token); found != "" {
			token = found
		}
	}
	if !tokenRE.MatchString(token) {
		return "", &APIError{Code: "invalid_access_token", Message: "未识别到合法 JWT 格式 accessToken", Status: 400}
	}
	return token, nil
}

func accessTokenFromJSON(raw string) string {
	var value any
	if json.Unmarshal([]byte(raw), &value) != nil {
		return ""
	}
	return findAccessTokenValue(value)
}

func findAccessTokenValue(value any) string {
	switch v := value.(type) {
	case map[string]any:
		for _, key := range []string{"accessToken", "access_token"} {
			if token := normalizeAccessTokenString(stringValue(v[key])); tokenRE.MatchString(token) {
				return token
			}
		}
		for _, child := range v {
			if token := findAccessTokenValue(child); token != "" {
				return token
			}
		}
	case []any:
		for _, child := range v {
			if token := findAccessTokenValue(child); token != "" {
				return token
			}
		}
	}
	return ""
}

func ExtractAccessTokens(raw string) ([]string, error) {
	text := strings.TrimSpace(raw)
	if text == "" {
		return nil, &APIError{Code: "missing_credential", Message: "请填写 accessToken 或 /api/auth/session JSON", Status: 400}
	}
	seen := map[string]struct{}{}
	out := []string{}
	add := func(token string) {
		token = normalizeAccessTokenString(token)
		if !tokenRE.MatchString(token) {
			return
		}
		if _, ok := seen[token]; ok {
			return
		}
		seen[token] = struct{}{}
		out = append(out, token)
	}
	if strings.HasPrefix(text, "{") || strings.HasPrefix(text, "[") {
		var value any
		if json.Unmarshal([]byte(text), &value) == nil {
			collectAccessTokens(value, add)
			if len(out) > 0 {
				return out, nil
			}
			return nil, &APIError{Code: "invalid_session_json", Message: "Session JSON 未找到 accessToken", Status: 400}
		}
	}
	for _, token := range tokenFindRE.FindAllString(text, -1) {
		add(token)
	}
	if len(out) == 0 {
		return nil, &APIError{Code: "invalid_access_token", Message: "未识别到合法 JWT 格式 accessToken", Status: 400}
	}
	return out, nil
}

func collectAccessTokens(value any, add func(string)) {
	switch v := value.(type) {
	case map[string]any:
		for _, key := range []string{"accessToken", "access_token"} {
			add(stringValue(v[key]))
		}
		for _, child := range v {
			collectAccessTokens(child, add)
		}
	case []any:
		for _, child := range v {
			collectAccessTokens(child, add)
		}
	case string:
		for _, token := range tokenFindRE.FindAllString(v, -1) {
			add(token)
		}
	}
}

func normalizeAccessTokenString(raw string) string {
	return strings.Trim(strings.TrimSpace(raw), `"' ,;`)
}

func NormalizeProxyCandidates(raw string) ([]string, error) {
	value := strings.TrimSpace(raw)
	if value == "" {
		return nil, &APIError{Code: "missing_proxy", Message: "请填写代理", Status: 400}
	}
	if len(value) > 4096 {
		return nil, &APIError{Code: "proxy_too_large", Message: "代理配置过长，已拒绝处理", Status: 400}
	}
	if parts := splitProxyList(value); len(parts) > 1 {
		out := make([]string, 0, len(parts))
		for _, part := range parts {
			got, err := NormalizeProxyCandidates(part)
			if err != nil {
				return nil, err
			}
			out = append(out, got...)
		}
		if len(out) > 64 {
			out = out[:64]
		}
		return uniqueStrings(out...), nil
	}
	if len(value) > 512 {
		return nil, &APIError{Code: "proxy_too_large", Message: "单条代理配置过长，已拒绝处理", Status: 400}
	}

	scheme := ""
	if idx := strings.Index(value, "://"); idx > 0 {
		scheme = strings.ToLower(value[:idx])
		value = strings.TrimSpace(value[idx+3:])
		if scheme != "http" && scheme != "https" && scheme != "socks5" && scheme != "socks5h" {
			return nil, &APIError{Code: "invalid_proxy", Message: "代理协议不正确", Status: 400}
		}
	}

	if !strings.Contains(value, "@") {
		parts := strings.Split(value, ":")
		if len(parts) >= 4 && isDigits(parts[1]) {
			host, port, user := strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1]), strings.TrimSpace(parts[2])
			pass := strings.TrimSpace(strings.Join(parts[3:], ":"))
			if host != "" && port != "" && user != "" && pass != "" {
				value = fmt.Sprintf("%s:%s@%s:%s", user, pass, host, port)
			}
		}
	}

	hostPart := value
	if at := strings.LastIndex(value, "@"); at >= 0 {
		hostPart = value[at+1:]
	}
	if !strings.Contains(hostPart, ":") {
		return nil, &APIError{Code: "proxy_missing_port", Message: "代理配置中未检测到端口号", Status: 400}
	}
	port := hostPart[strings.LastIndex(hostPart, ":")+1:]
	if slash := strings.Index(port, "/"); slash >= 0 {
		port = port[:slash]
	}
	if !isDigits(port) {
		return nil, &APIError{Code: "proxy_invalid_port", Message: "代理配置中的端口号必须为纯数字", Status: 400}
	}

	if scheme != "" {
		return []string{scheme + "://" + value}, nil
	}
	return []string{"socks5h://" + value}, nil
}

func splitProxyList(value string) []string {
	rawParts := regexp.MustCompile(`[\n\r,;]+`).Split(value, -1)
	out := make([]string, 0, len(rawParts))
	for _, part := range rawParts {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}

func RotateProxyCandidates(candidates []string, key string) []string {
	candidates = uniqueStrings(candidates...)
	if len(candidates) < 2 {
		return candidates
	}
	key = strings.TrimSpace(key)
	if key == "" {
		return candidates
	}
	var sum uint32
	for _, ch := range key {
		sum = sum*33 + uint32(ch)
	}
	offset := int(sum % uint32(len(candidates)))
	out := append([]string{}, candidates[offset:]...)
	out = append(out, candidates[:offset]...)
	return out
}

func ExpandProxyRotations(candidates []string, rotations int) []string {
	if rotations < 1 {
		rotations = 1
	}
	out := make([]string, 0, len(candidates)*rotations*3)
	seed := time.Now().UnixNano()
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		out = append(out, candidate)
		out = append(out, proxyRegionVariant(candidate, "JP", seed), proxyRegionVariant(candidate, "US", seed), proxyRegionVariant(candidate, "ID", seed))
		for i := 1; i < rotations; i++ {
			out = append(out, rotateProxySession(candidate, seed+int64(i)))
			out = append(out, proxyRegionVariant(candidate, "US", seed+int64(i)))
		}
	}
	return uniqueStrings(out...)
}

func ProviderStageProxy(raw string) string {
	return raw
}

func ProviderProxyCandidates(raw string, rotations int) []string {
	if rotations < 1 {
		rotations = 1
	}
	seed := time.Now().UnixNano()
	jp := proxyRegionVariant(raw, "JP", seed)
	out := []string{raw, jp}
	base := jp
	for i := 1; i < rotations; i++ {
		out = append(out, rotateProxySession(base, seed+int64(i)))
	}
	out = append(out, proxyRegionVariant(raw, "US", seed+1), proxyRegionVariant(raw, "ID", seed+2))
	return uniqueStrings(out...)
}

func rotateProxySession(raw string, nonce int64) string {
	u, err := url.Parse(raw)
	if err != nil || u.User == nil {
		return raw
	}
	user := u.User.Username()
	pass, _ := u.User.Password()
	rotatedUser := rotateProxyUsername(user, nonce)
	rotatedPass := rotateKookeeyPassword(pass, nonce)
	if rotatedUser == user && rotatedPass == pass {
		return raw
	}
	u.User = url.UserPassword(rotatedUser, rotatedPass)
	return u.String()
}

func rotateProxyUsername(user string, nonce int64) string {
	user = strings.TrimSpace(user)
	if user == "" {
		return user
	}
	newSID := fmt.Sprintf("sid-%x", uint64(nonce))
	if strings.Contains(user, "sid--") {
		return strings.ReplaceAll(user, "sid--", newSID+"-")
	}
	if regexp.MustCompile(`sid-[A-Za-z0-9_]+-t-`).MatchString(user) {
		return regexp.MustCompile(`sid-[A-Za-z0-9_]+-t-`).ReplaceAllString(user, newSID+"-t-")
	}
	if strings.Contains(user, "sid-") {
		return regexp.MustCompile(`sid-[A-Za-z0-9_-]*`).ReplaceAllString(user, newSID)
	}
	return user
}

func proxyRegionVariant(raw, region string, nonce int64) string {
	region = strings.ToUpper(strings.TrimSpace(region))
	if region == "" {
		return raw
	}
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return raw
	}
	host := u.Hostname()
	port := u.Port()
	user := ""
	pass := ""
	if u.User != nil {
		user = u.User.Username()
		pass, _ = u.User.Password()
	}
	changed := false
	if strings.Contains(strings.ToLower(host), "kookeey.info") {
		if port == "" {
			port = "1000"
		}
		host = "gate-" + strings.ToLower(region) + ".kookeey.info"
		pass2 := replaceTrailingRegion(pass, region)
		user2 := replaceTrailingRegion(user, region)
		if pass2 != pass || user2 != user || host != u.Hostname() {
			pass, user = pass2, user2
			changed = true
		}
	}
	if strings.Contains(strings.ToLower(host), "cliproxy.io") {
		user2 := regexp.MustCompile(`region-[A-Za-z]{2}`).ReplaceAllString(user, "region-"+region)
		user2 = rotateProxyUsername(user2, nonce)
		if user2 != user {
			user = user2
			changed = true
		}
	}
	if !changed {
		return raw
	}
	u.Host = host
	if port != "" {
		u.Host = host + ":" + port
	}
	if user != "" || pass != "" {
		u.User = url.UserPassword(user, pass)
	}
	return u.String()
}

func replaceTrailingRegion(value, region string) string {
	if value == "" {
		return value
	}
	if regexp.MustCompile(`-[A-Za-z]{2}(-\d{6,12})?$`).MatchString(value) {
		return regexp.MustCompile(`-[A-Za-z]{2}(-\d{6,12})?$`).ReplaceAllString(value, "-"+region+"$1")
	}
	return value
}

func rotateKookeeyPassword(pass string, nonce int64) string {
	pass = strings.TrimSpace(pass)
	if pass == "" {
		return pass
	}
	session := fmt.Sprintf("%08d", uint64(nonce)%100000000)
	if regexp.MustCompile(`-[A-Za-z]{2}-\d{6,12}$`).MatchString(pass) {
		return regexp.MustCompile(`-\d{6,12}$`).ReplaceAllString(pass, "-"+session)
	}
	if regexp.MustCompile(`-[A-Za-z]{2}$`).MatchString(pass) {
		return pass + "-" + session
	}
	return pass
}

func MaskProxy(raw string) string {
	out := raw
	out = regexp.MustCompile(`(socks5h?|https?)://([^:@/\s]+):([^@/\s]+)@`).ReplaceAllString(out, `$1://<redacted>:<redacted>@`)
	out = regexp.MustCompile(`(^|[^A-Za-z0-9_.-])([A-Za-z0-9_.-]+):(\d{2,5}):([^:\s@]+):([^\s@]+)`).ReplaceAllString(out, `$1$2:$3:<redacted>:<redacted>`)
	return out
}

func proxyScheme(raw string) string {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme == "" {
		return "unknown"
	}
	return u.Scheme
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, ch := range s {
		if ch < '0' || ch > '9' {
			return false
		}
	}
	return true
}
