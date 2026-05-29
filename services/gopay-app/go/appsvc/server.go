package appsvc

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/byte-v-forge/gpt/gopay/pb"
	"github.com/byte-v-forge/gpt/gopay/protocol"
	gopayapp "github.com/byte-v-forge/gpt/gopay/protocol/app"
)

type Server struct {
	pb.UnimplementedGopayAppServiceServer
	cfg               Config
	store             *StateStore
	checkPhoneProxyMu sync.Mutex
	checkPhoneProxyIx int
}

func NewServer(cfg Config) (*Server, error) {
	store, err := NewStateStore(cfg.StateDSN, cfg.StateTable)
	if err != nil {
		return nil, err
	}
	return &Server{cfg: cfg, store: store}, nil
}

func (s *Server) GetGoPayState(ctx context.Context, req *pb.GetGoPayStateRequest) (*pb.GetGoPayStateResponse, error) {
	key, err := NormalizeStateKey(req.GetUserId())
	if err != nil {
		return &pb.GetGoPayStateResponse{Success: false, ErrorMessage: err.Error()}, nil
	}
	raw, err := s.store.Load(ctx, key)
	if err != nil {
		return &pb.GetGoPayStateResponse{Success: false, ErrorMessage: err.Error()}, nil
	}
	return &pb.GetGoPayStateResponse{Success: true, UserId: key, StateJson: raw}, nil
}

func (s *Server) UpsertGoPayState(ctx context.Context, req *pb.UpsertGoPayStateRequest) (*pb.UpsertGoPayStateResponse, error) {
	key, err := NormalizeStateKey(req.GetUserId())
	if err != nil {
		return &pb.UpsertGoPayStateResponse{Success: false, ErrorMessage: err.Error()}, nil
	}
	raw, err := s.store.Save(ctx, key, firstNonEmpty(req.GetStateJson(), "{}"))
	if err != nil {
		return &pb.UpsertGoPayStateResponse{Success: false, ErrorMessage: err.Error()}, nil
	}
	return &pb.UpsertGoPayStateResponse{Success: true, UserId: key, StateJson: raw}, nil
}

func (s *Server) DeleteGoPayState(ctx context.Context, req *pb.DeleteGoPayStateRequest) (*pb.DeleteGoPayStateResponse, error) {
	key, err := NormalizeStateKey(req.GetUserId())
	if err != nil {
		return &pb.DeleteGoPayStateResponse{Success: false, ErrorMessage: err.Error()}, nil
	}
	if err := s.store.Delete(ctx, key); err != nil {
		return &pb.DeleteGoPayStateResponse{Success: false, ErrorMessage: err.Error()}, nil
	}
	return &pb.DeleteGoPayStateResponse{Success: true}, nil
}

func (s *Server) parseRequestState(raw string) stateMap {
	state, err := parseState(raw)
	if err != nil {
		return stateMap{"last_error": err.Error()}
	}
	return state
}

func (s *Server) authBody(extra map[string]any) map[string]any {
	body := map[string]any{}
	for key, value := range extra {
		body[key] = value
	}
	body["client_id"] = s.cfg.GotoClientID
	body["client_secret"] = s.cfg.GotoClientSecret
	return body
}

func (s *Server) pin(value string) string {
	return strings.TrimSpace(value)
}

func (s *Server) signupProfile(phone, name, email string) (string, string) {
	resolvedName := strings.TrimSpace(name)
	resolvedEmail := strings.TrimSpace(email)
	if resolvedName != "" {
		return resolvedName, resolvedEmail
	}
	return signupNameFromSeed(signupSeed(phone)), resolvedEmail
}

func (s *Server) signupBasicAuthorization() string {
	return "Basic " + base64.StdEncoding.EncodeToString([]byte(s.cfg.SignupAuthUUID))
}

func (s *Server) newClient(ctx context.Context, token string, proxyURL string, device gopayapp.DeviceFingerprint) (*gopayapp.Client, error) {
	cfg := gopayapp.ConfigFromEnv(token)
	cfg.ProxyURL = proxyURL
	cfg.Timeout = 30 * time.Second
	cfg.Device = device
	cfg.Logger = func(ctx context.Context, message string, fields map[string]any) {
		fmt.Printf("[gopay-app] %s %v\n", message, fields)
	}
	return gopayapp.NewClient(cfg)
}

func (s *Server) clientForState(ctx context.Context, state stateMap) (*gopayapp.Client, error) {
	refresh := s.ensureAccessToken(ctx, state, s.cfg.TokenRefreshMinTTL, false)
	if !anyBool(refresh["success"]) && !tokenUsable(state, "token", 0) {
		return nil, fmt.Errorf("%s", firstNonEmpty(anyString(refresh["error"]), "token refresh failed"))
	}
	device, err := s.ensureDevice(state)
	if err != nil {
		return nil, err
	}
	return s.newClient(ctx, stateString(state, "token"), s.proxyForState(state), device)
}

func (s *Server) tmpClientForState(ctx context.Context, state stateMap) (*gopayapp.Client, error) {
	token := stateString(state, "_tmp_token")
	if token == "" {
		return nil, fmt.Errorf("temporary account token missing")
	}
	if !tmpTokenUsable(state, 0) {
		expiresAt := firstNonZero(jwtExpiresAt(token), stateInt(state, "_tmp_token_expires_at"))
		return nil, fmt.Errorf("temporary account token expired: expires_at=%d", expiresAt)
	}
	device, err := s.ensureDevice(state)
	if err != nil {
		return nil, err
	}
	return s.newClient(ctx, token, s.proxyForState(state), device)
}

func (s *Server) proxyForAttempt(attempt int, state stateMap) (string, int, int, error) {
	if len(s.cfg.DynamicEgress) == 0 {
		return "", 0, 0, fmt.Errorf("GoPay dynamic egress config is required")
	}
	if attempt < 1 {
		attempt = 1
	}
	base := s.proxyIndex(stateString(state, "_gopay_proxy_attempt_base"))
	if base < 0 {
		base = s.proxyIndex(stateString(state, "_gopay_proxy"))
	}
	if base < 0 {
		base = 0
	}
	index := (base + attempt - 1) % len(s.cfg.DynamicEgress)
	if state != nil {
		state["_gopay_proxy_attempt_base"] = s.cfg.DynamicEgress[base]
	}
	if state != nil && attempt > 1 && index == base {
		state["_gopay_proxy_reused_with_rotated_session"] = true
		state["_gopay_proxy_reuse_attempt"] = attempt
	}
	proxyURL := s.cfg.DynamicEgress[index]
	if state != nil {
		state["_gopay_proxy"] = proxyURL
	}
	return proxyURL, index + 1, len(s.cfg.DynamicEgress), nil
}

func (s *Server) rotateLoginAttemptIdentity(ctx context.Context, state stateMap) error {
	if state == nil {
		return nil
	}
	sessionData, err := s.createProxyRuntimeSession(ctx)
	if err != nil {
		return err
	}
	for key, value := range sessionData {
		state[key] = value
	}
	if len(sessionData) > 0 {
		state["_proxy_runtime_session_rotated_for_login"] = true
	}
	_, rawDevice, err := s.newLogonDevice()
	if err != nil {
		return err
	}
	state["device"] = rawDevice
	state["_device_rotated_for_login"] = true
	return nil
}

func (s *Server) proxyForState(state stateMap) string {
	if len(s.cfg.DynamicEgress) == 0 {
		return ""
	}
	index := s.proxyIndex(stateString(state, "_gopay_proxy"))
	if index < 0 {
		proxyURL, _, _, _ := s.proxyForAttempt(1, state)
		return proxyURL
	}
	return s.cfg.DynamicEgress[index]
}

func (s *Server) nextCheckPhoneProxyState() stateMap {
	state := stateMap{}
	if len(s.cfg.DynamicEgress) == 0 {
		return state
	}
	s.checkPhoneProxyMu.Lock()
	index := s.checkPhoneProxyIx % len(s.cfg.DynamicEgress)
	s.checkPhoneProxyIx = (index + 1) % len(s.cfg.DynamicEgress)
	s.checkPhoneProxyMu.Unlock()
	state["_gopay_proxy"] = s.cfg.DynamicEgress[index]
	return state
}

func (s *Server) generateDeviceProxyState(ctx context.Context) (stateMap, error) {
	state := s.nextCheckPhoneProxyState()
	sessionData, err := s.createProxyRuntimeSession(ctx)
	if err != nil {
		return state, err
	}
	for key, value := range sessionData {
		state[key] = value
	}
	_, rawDevice, err := s.newLogonDevice()
	if err != nil {
		return state, err
	}
	state["device"] = rawDevice
	return state, nil
}

func (s *Server) deviceProxyDiagnostics(state stateMap) map[string]any {
	data := map[string]any{
		"dynamic_egress_size": len(s.cfg.DynamicEgress),
	}
	proxyURL := stateString(state, "_gopay_proxy")
	if proxyURL != "" {
		hash := sha256.Sum256([]byte(proxyURL))
		data["proxy_hash"] = hex.EncodeToString(hash[:])[:12]
	}
	if index := s.proxyIndex(proxyURL); index >= 0 {
		data["proxy_slot"] = index + 1
	}
	if hash := stateString(state, "_proxy_runtime_session_hash"); hash != "" {
		data["proxy_runtime_session_hash"] = hash
		data["proxy_runtime_pool_endpoints"] = anyInt(state["_proxy_runtime_pool_endpoints"])
	}
	if rotated, ok := state["_proxy_runtime_session_rotated"].(bool); ok {
		data["proxy_runtime_session_rotated"] = rotated
	}
	if fp := deviceFingerprintForState(state); fp != "" {
		data["device_fingerprint"] = fp
	}
	return data
}

func deviceFingerprintForState(state stateMap) string {
	device := nestedMap(state["device"])
	if len(device) == 0 {
		return ""
	}
	out := []string{}
	addPlain := func(label, key string) {
		if value := anyString(device[key]); value != "" {
			out = append(out, label+"="+value)
		}
	}
	addHash := func(label, key string) {
		if value := anyString(device[key]); value != "" {
			out = append(out, label+"#"+shortHash(value))
		}
	}
	addPlain("profile", "profile_id")
	addPlain("make", "x-phonemake")
	addPlain("model", "x-phonemodel")
	addPlain("os", "x-deviceos")
	addPlain("screen", "m1_screen")
	addPlain("tls", "tls_profile")
	addHash("uid", "x-uniqueid")
	addHash("session", "x-session-id")
	addHash("tx", "transaction-id")
	addHash("d1", "d1")
	addHash("conn", "m1_connection_id")
	addHash("widevine", "m1_widevine_id")
	addHash("wifi", "m1_wifi_mac")
	addHash("ssid", "m1_wifi_ssid")
	addHash("sig", "m1_signature")
	addHash("sig_time", "m1_signature_time")
	addHash("firebase", "m1_firebase_app_instance_id")
	addHash("uuid", "m1_device_uuid")
	addHash("adid", "advertising_id")
	addHash("appset", "app_set_id")
	addHash("devtoken", "x-devicetoken")
	addHash("imei", "x-imei")
	addHash("ip", "x-ipaddress")
	if parsed := deviceFromMap(device); parsed.AppID != "" {
		out = append(out, "x_m1#"+shortHash(parsed.XM1()))
	}
	return strings.Join(out, "/")
}

func shortHash(value string) string {
	hash := sha256.Sum256([]byte(value))
	return hex.EncodeToString(hash[:])[:12]
}

func (s *Server) proxyIndex(value string) int {
	value = strings.TrimSpace(value)
	for index, item := range s.cfg.DynamicEgress {
		if strings.TrimSpace(item) == value {
			return index
		}
	}
	return -1
}

func (s *Server) ensureDevice(state stateMap) (gopayapp.DeviceFingerprint, error) {
	raw := nestedMap(state["device"])
	if len(raw) > 0 {
		device := deviceFromMap(raw)
		if deviceNeedsBackfill(device) {
			next, err := gopayapp.NewDeviceFingerprint(gopayapp.DeviceConfigFromEnv())
			if err != nil {
				return gopayapp.DeviceFingerprint{}, err
			}
			device = mergeDevice(device, next)
		}
		state["device"] = deviceToMap(device)
		return device, nil
	}
	device, err := gopayapp.NewDeviceFingerprint(gopayapp.DeviceConfigFromEnv())
	if err != nil {
		return gopayapp.DeviceFingerprint{}, err
	}
	rawID := make([]byte, 8)
	_, _ = rand.Read(rawID)
	out := deviceToMap(device)
	out["profile_id"] = hex.EncodeToString(rawID)
	out["profile_created_at"] = time.Now().Unix()
	state["device"] = out
	return device, nil
}

func (s *Server) newLogonDevice() (gopayapp.DeviceFingerprint, map[string]any, error) {
	device, err := gopayapp.NewDeviceFingerprint(gopayapp.DeviceConfigFromEnv())
	if err != nil {
		return gopayapp.DeviceFingerprint{}, nil, err
	}
	out := deviceToMap(device)
	rawID := make([]byte, 8)
	_, _ = rand.Read(rawID)
	out["profile_id"] = hex.EncodeToString(rawID)
	out["profile_created_at"] = time.Now().Unix()
	return device, out, nil
}

func deviceNeedsBackfill(device gopayapp.DeviceFingerprint) bool {
	return device.AppID == "" ||
		device.UniqueID == "" ||
		device.TLSProfileName == "" ||
		device.M1Hardware == "" ||
		device.IMEI == "" ||
		device.IPAddress == "" ||
		device.FirebaseID == "" ||
		device.AdvertisingID == "" ||
		device.AppSetID == "" ||
		device.M1SignatureTime == ""
}

func apiError(label string, resp *protocol.Response) string {
	if resp == nil {
		return label + ": no response"
	}
	if resp.StatusCode == http.StatusUnauthorized {
		return "AUTH_INVALID"
	}
	return fmt.Sprintf("%s: status %d %s", label, resp.StatusCode, compactErrorDetail(resp.Payload))
}

func responseErrors(resp *protocol.Response) []any {
	if resp == nil {
		return nil
	}
	for _, source := range []any{resp.Payload["errors"], resp.Data()["errors"]} {
		if items, ok := source.([]any); ok {
			return items
		}
	}
	return nil
}

func responseText(resp *protocol.Response) string {
	if resp == nil {
		return ""
	}
	return string(resp.Body)
}

func isRateLimited(resp *protocol.Response) bool {
	if resp == nil {
		return false
	}
	if resp.StatusCode == http.StatusTooManyRequests {
		return true
	}
	for _, err := range responseErrors(resp) {
		text := strings.ToLower(compactErrorDetail(err))
		if strings.Contains(text, "ratelimited") {
			return true
		}
	}
	return false
}

func loginMethodsInvalidUser(resp *protocol.Response) bool {
	if resp == nil || resp.StatusCode != http.StatusUnauthorized {
		return false
	}
	for _, err := range responseErrors(resp) {
		text := strings.ToLower(compactErrorDetail(err))
		if strings.Contains(text, "invalid user") || strings.Contains(text, "could not find the user") {
			return true
		}
	}
	return false
}

func firstNonZero(values ...int64) int64 {
	for _, value := range values {
		if value != 0 {
			return value
		}
	}
	return 0
}
