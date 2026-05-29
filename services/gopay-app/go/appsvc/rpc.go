package appsvc

import (
	"context"
	"fmt"
	"strings"

	"github.com/byte-v-forge/gpt/gopay/pb"
)

func (s *Server) GenerateDeviceProxy(ctx context.Context, _ *pb.GenerateDeviceProxyRequest) (*pb.GenerateDeviceProxyResponse, error) {
	state, err := s.generateDeviceProxyState(ctx)
	data := s.deviceProxyDiagnostics(state)
	if err != nil {
		return &pb.GenerateDeviceProxyResponse{Success: false, ErrorMessage: err.Error(), StateJson: stateJSON(state)}, nil
	}
	return &pb.GenerateDeviceProxyResponse{
		Success:           true,
		ProxySlot:         int32(anyInt(data["proxy_slot"])),
		DynamicEgressSize: int32(anyInt(data["dynamic_egress_size"])),
		ProxyHash:         anyString(data["proxy_hash"]),
		DeviceFingerprint: anyString(data["device_fingerprint"]),
		StateJson:         stateJSON(state),
	}, nil
}

func (s *Server) CheckPhone(ctx context.Context, req *pb.CheckPhoneRequest) (*pb.CheckPhoneResponse, error) {
	phone := normalizePhoneWithConfig(s.cfg, req.GetPhone(), req.GetCountryCode())
	if phone == "" {
		return &pb.CheckPhoneResponse{Available: false, Status: "error", ErrorMessage: "phone required"}, nil
	}
	if strings.TrimSpace(req.GetStateJson()) == "" {
		return &pb.CheckPhoneResponse{Available: false, Status: "error", ErrorMessage: "generated device proxy state_json required"}, nil
	}
	proxyState := s.parseRequestState(req.GetStateJson())
	if stateString(proxyState, "_gopay_proxy") == "" {
		return &pb.CheckPhoneResponse{Available: false, Status: "error", ErrorMessage: "generated proxy missing", StateJson: stateJSON(proxyState)}, nil
	}
	if len(nestedMap(proxyState["device"])) == 0 {
		return &pb.CheckPhoneResponse{Available: false, Status: "error", ErrorMessage: "generated device missing", StateJson: stateJSON(proxyState)}, nil
	}
	result := s.checkPhoneByLoginMethods(ctx, phone, req.GetCountryCode(), proxyState)
	status := firstNonEmpty(anyString(result["status"]), "error")
	errorMessage := anyString(result["error"])
	if status == "registered" {
		errorMessage = "PHONE_REGISTERED"
	}
	return &pb.CheckPhoneResponse{
		Available:         anyBool(result["available"]),
		Status:            status,
		ErrorMessage:      errorMessage,
		ProxySlot:         int32(anyInt(result["proxy_slot"])),
		DynamicEgressSize: int32(anyInt(result["dynamic_egress_size"])),
		ProxyHash:         anyString(result["proxy_hash"]),
		DeviceFingerprint: anyString(result["device_fingerprint"]),
		StateJson:         anyString(result["state_json"]),
	}, nil
}

func (s *Server) LoginStart(ctx context.Context, req *pb.LoginStartRequest) (*pb.LoginStartResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	if s.expireLoginIfNeeded(state) {
		// state mutated only; caller receives state_json below.
	}
	stage := stateString(state, "stage")
	if stage == "ready" || stage == "consumed" {
		tokenCheck := s.checkTokenValid(ctx, state)
		if s.tokenCheckReady(tokenCheck) {
			return &pb.LoginStartResponse{Success: true, OtpSent: false, StateJson: stateJSON(state)}, nil
		}
		if s.tokenCheckValid(tokenCheck) {
			return &pb.LoginStartResponse{Success: false, ErrorMessage: s.tokenCheckError(tokenCheck), StateJson: stateJSON(state)}, nil
		}
	}
	phone := strings.TrimSpace(req.GetPhone())
	if phone == "" && (stage == "login" || stage == "login_otp_pending") {
		phone = stateString(state, "_login_phone")
	}
	if phone == "" {
		return &pb.LoginStartResponse{Success: false, ErrorMessage: "login phone required", StateJson: stateJSON(state)}, nil
	}
	if stage == "login_otp_pending" && stateString(state, "_login_otp_token") != "" && stateString(state, "_login_verification_id") != "" {
		return &pb.LoginStartResponse{Success: true, OtpSent: true, VerificationId: stateString(state, "_login_verification_id"), VerificationMethod: stateString(state, "_login_verification_method"), StateJson: stateJSON(state)}, nil
	}
	result := s.startLogin(ctx, state, phone, req.GetPin(), req.GetCountryCode(), req.GetOtpChannel())
	if !anyBool(result["success"]) {
		errMessage := anyString(result["error"])
		if anyBool(result["not_registered"]) {
			errMessage = "GOPAY_PHONE_NOT_REGISTERED"
		}
		return &pb.LoginStartResponse{Success: false, ErrorMessage: errMessage, StateJson: stateJSON(state)}, nil
	}
	if anyBool(result["ready"]) {
		tokenCheck := s.checkTokenValid(ctx, state)
		if !s.tokenCheckReady(tokenCheck) {
			return &pb.LoginStartResponse{Success: false, ErrorMessage: s.tokenCheckError(tokenCheck), StateJson: stateJSON(state)}, nil
		}
	}
	return &pb.LoginStartResponse{
		Success:            true,
		OtpSent:            anyBool(result["otp_sent"]),
		VerificationId:     anyString(result["verification_id"]),
		VerificationMethod: anyString(result["method"]),
		StateJson:          stateJSON(state),
	}, nil
}

func (s *Server) LoginComplete(ctx context.Context, req *pb.LoginCompleteRequest) (*pb.LoginCompleteResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireLoginIfNeeded(state)
	if stateString(state, "stage") != "login_otp_pending" {
		return &pb.LoginCompleteResponse{Success: false, ErrorMessage: fmt.Sprintf("not waiting for login otp: %s", firstNonEmpty(stateString(state, "stage"), "idle")), StateJson: stateJSON(state)}, nil
	}
	if err := s.completeLogin(ctx, state, req.GetOtp()); err != nil {
		return &pb.LoginCompleteResponse{Success: false, ErrorMessage: err.Error(), StateJson: stateJSON(state)}, nil
	}
	stage := firstNonEmpty(stateString(state, "stage"), "idle")
	return &pb.LoginCompleteResponse{
		Success:            true,
		Phone:              firstNonEmpty(stateString(state, "phone"), stateString(state, "_login_phone")),
		OtpSent:            stage == "login_otp_pending",
		VerificationId:     stateString(state, "_login_verification_id"),
		VerificationMethod: stateString(state, "_login_verification_method"),
		StateJson:          stateJSON(state),
	}, nil
}

func (s *Server) SignupStart(ctx context.Context, req *pb.SignupStartRequest) (*pb.SignupStartResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireLoginIfNeeded(state)
	s.expireSignupIfNeeded(state)
	if strings.TrimSpace(req.GetPhone()) == "" {
		return &pb.SignupStartResponse{Success: false, ErrorMessage: "signup phone required", StateJson: stateJSON(state)}, nil
	}
	result := s.startSignup(ctx, state, req.GetPhone(), req.GetName(), req.GetEmail(), req.GetCountryCode(), req.GetOtpChannel(), req.GetSkipPhoneProbe())
	if !anyBool(result["success"]) {
		return &pb.SignupStartResponse{Success: false, ErrorMessage: anyString(result["error"]), RawJson: anyString(result["raw_json"]), StateJson: stateJSON(state)}, nil
	}
	return &pb.SignupStartResponse{
		Success:            true,
		OtpSent:            anyBool(result["otp_sent"]),
		VerificationId:     anyString(result["verification_id"]),
		VerificationMethod: anyString(result["method"]),
		RawJson:            anyString(result["raw_json"]),
		RetryTimerSeconds:  int32Slice(result["retry_timer_seconds"]),
		StateJson:          stateJSON(state),
	}, nil
}

func (s *Server) SignupRetry(ctx context.Context, req *pb.SignupRetryRequest) (*pb.SignupRetryResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.retrySignupOTP(ctx, state)
	return &pb.SignupRetryResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), OtpSent: anyBool(result["otp_sent"]), RawJson: anyString(result["raw_json"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) SignupComplete(ctx context.Context, req *pb.SignupCompleteRequest) (*pb.SignupCompleteResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireSignupIfNeeded(state)
	result := s.completeSignup(ctx, state, req.GetOtp())
	return &pb.SignupCompleteResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), Phone: anyString(result["phone"]), PinSetupRequired: anyBool(result["pin_setup_required"]), RawJson: anyString(result["raw_json"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) CreatePinStart(ctx context.Context, req *pb.CreatePinStartRequest) (*pb.CreatePinStartResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireSignupIfNeeded(state)
	result := s.startSignupPIN(ctx, state, req.GetPin(), req.GetOtpChannel())
	return &pb.CreatePinStartResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), OtpSent: anyBool(result["otp_sent"]), VerificationId: anyString(result["verification_id"]), VerificationMethod: anyString(result["method"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) CreatePinRetry(ctx context.Context, req *pb.CreatePinRetryRequest) (*pb.CreatePinRetryResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireSignupIfNeeded(state)
	result := s.retrySignupPIN(ctx, state)
	return &pb.CreatePinRetryResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), OtpSent: anyBool(result["otp_sent"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) CreatePinComplete(ctx context.Context, req *pb.CreatePinCompleteRequest) (*pb.CreatePinCompleteResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireSignupIfNeeded(state)
	result := s.completeSignupPIN(ctx, state, req.GetOtp(), req.GetPin())
	return &pb.CreatePinCompleteResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), Phone: anyString(result["phone"]), PinSetupComplete: anyBool(result["pin_setup_complete"]), StateJson: stateJSON(state)}, nil
}
