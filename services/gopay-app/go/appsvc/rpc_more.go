package appsvc

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/byte-v-forge/gpt/gopay/pb"
)

func (s *Server) AuthStart(ctx context.Context, req *pb.AuthStartRequest) (*pb.AuthStartResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireLoginIfNeeded(state)
	s.expireSignupIfNeeded(state)
	stage := stateString(state, "stage")
	if stage == "signup_pin_required" || stage == "signup_pin_otp_pending" {
		tokenCheck := s.checkTokenValid(ctx, state)
		state["stage"] = stage
		if !s.tokenCheckValid(tokenCheck) {
			return &pb.AuthStartResponse{Success: false, ErrorMessage: s.tokenCheckError(tokenCheck), Mode: "signup", Stage: stage, PinSetupRequired: true, StateJson: stateJSON(state)}, nil
		}
		return &pb.AuthStartResponse{Success: true, Mode: "signup", Stage: stage, OtpSent: stage == "signup_pin_otp_pending", VerificationId: stateString(state, "_signup_pin_verification_id"), VerificationMethod: stateString(state, "_signup_pin_verification_method"), PinSetupRequired: true, StateJson: stateJSON(state)}, nil
	}
	tokenCheck := s.checkTokenValid(ctx, state)
	if s.tokenCheckValid(tokenCheck) {
		return &pb.AuthStartResponse{Success: true, Mode: "token", Stage: firstNonEmpty(stateString(state, "stage"), "ready"), Ready: true, StateJson: stateJSON(state)}, nil
	}
	switch stage {
	case "login_otp_pending":
		return &pb.AuthStartResponse{Success: true, Mode: "login", Stage: stage, OtpSent: true, VerificationId: stateString(state, "_login_verification_id"), VerificationMethod: stateString(state, "_login_verification_method"), StateJson: stateJSON(state)}, nil
	case "signup_otp_pending":
		return &pb.AuthStartResponse{Success: true, Mode: "signup", Stage: stage, OtpSent: true, VerificationId: stateString(state, "_signup_verification_id"), VerificationMethod: stateString(state, "_signup_verification_method"), StateJson: stateJSON(state)}, nil
	case "signup_pin_required":
		return &pb.AuthStartResponse{Success: true, Mode: "signup", Stage: stage, PinSetupRequired: true, StateJson: stateJSON(state)}, nil
	}
	phone := strings.TrimSpace(req.GetPhone())
	if phone == "" {
		return &pb.AuthStartResponse{Success: false, ErrorMessage: "auth phone required", StateJson: stateJSON(state)}, nil
	}
	login := s.startLogin(ctx, state, phone, req.GetPin(), req.GetCountryCode(), req.GetOtpChannel())
	if anyBool(login["success"]) {
		ready := anyBool(login["ready"])
		if ready {
			tokenCheck := s.checkTokenValid(ctx, state)
			if !s.tokenCheckValid(tokenCheck) {
				return &pb.AuthStartResponse{Success: false, ErrorMessage: s.tokenCheckError(tokenCheck), Mode: "login", Stage: firstNonEmpty(stateString(state, "stage"), "idle"), StateJson: stateJSON(state)}, nil
			}
		}
		return &pb.AuthStartResponse{Success: true, Mode: "login", Stage: firstNonEmpty(stateString(state, "stage"), "idle"), OtpSent: anyBool(login["otp_sent"]), VerificationId: anyString(login["verification_id"]), VerificationMethod: anyString(login["method"]), Ready: ready, StateJson: stateJSON(state)}, nil
	}
	if !anyBool(login["not_registered"]) {
		return &pb.AuthStartResponse{Success: false, ErrorMessage: anyString(login["error"]), Mode: "login", Stage: firstNonEmpty(stateString(state, "stage"), "idle"), StateJson: stateJSON(state)}, nil
	}
	signup := s.startSignup(ctx, state, phone, "", "", req.GetCountryCode(), req.GetOtpChannel(), false)
	return &pb.AuthStartResponse{
		Success:            anyBool(signup["success"]),
		ErrorMessage:       anyString(signup["error"]),
		Mode:               "signup",
		Stage:              firstNonEmpty(stateString(state, "stage"), "idle"),
		OtpSent:            anyBool(signup["otp_sent"]),
		VerificationId:     anyString(signup["verification_id"]),
		VerificationMethod: anyString(signup["method"]),
		StateJson:          stateJSON(state),
	}, nil
}

func (s *Server) AuthComplete(ctx context.Context, req *pb.AuthCompleteRequest) (*pb.AuthCompleteResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireLoginIfNeeded(state)
	s.expireSignupIfNeeded(state)
	stage := stateString(state, "stage")
	if stage == "ready" && tokenUsable(state, "token", 30*time.Second) {
		tokenCheck := s.checkTokenValid(ctx, state)
		if s.tokenCheckValid(tokenCheck) {
			return &pb.AuthCompleteResponse{Success: true, Mode: "token", Stage: "ready", Phone: stateString(state, "phone"), Ready: true, StateJson: stateJSON(state)}, nil
		}
		return &pb.AuthCompleteResponse{Success: false, ErrorMessage: s.tokenCheckError(tokenCheck), Mode: "token", Stage: firstNonEmpty(stateString(state, "stage"), "ready"), Phone: stateString(state, "phone"), StateJson: stateJSON(state)}, nil
	}
	switch stage {
	case "login_otp_pending":
		if err := s.completeLogin(ctx, state, req.GetOtp()); err != nil {
			return &pb.AuthCompleteResponse{Success: false, ErrorMessage: err.Error(), Mode: "login", Stage: firstNonEmpty(stateString(state, "stage"), "idle"), StateJson: stateJSON(state)}, nil
		}
		stage := firstNonEmpty(stateString(state, "stage"), "idle")
		ready := stage == "ready"
		return &pb.AuthCompleteResponse{
			Success:            true,
			Mode:               "login",
			Stage:              stage,
			Phone:              firstNonEmpty(stateString(state, "phone"), stateString(state, "_login_phone")),
			Ready:              ready,
			OtpSent:            stage == "login_otp_pending",
			VerificationId:     stateString(state, "_login_verification_id"),
			VerificationMethod: stateString(state, "_login_verification_method"),
			StateJson:          stateJSON(state),
		}, nil
	case "signup_otp_pending":
		result := s.completeSignup(ctx, state, req.GetOtp())
		ready := anyBool(result["success"]) && stateString(state, "stage") == "ready"
		return &pb.AuthCompleteResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), Mode: "signup", Stage: firstNonEmpty(stateString(state, "stage"), "idle"), Phone: anyString(result["phone"]), Ready: ready, PinSetupRequired: anyBool(result["pin_setup_required"]), StateJson: stateJSON(state)}, nil
	case "signup_pin_otp_pending":
		result := s.completeSignupPIN(ctx, state, req.GetOtp(), req.GetPin())
		ready := anyBool(result["success"]) && stateString(state, "stage") == "ready"
		return &pb.AuthCompleteResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), Mode: "signup", Stage: firstNonEmpty(stateString(state, "stage"), "idle"), Phone: anyString(result["phone"]), Ready: ready, PinSetupComplete: anyBool(result["pin_setup_complete"]), StateJson: stateJSON(state)}, nil
	default:
		return &pb.AuthCompleteResponse{Success: false, ErrorMessage: fmt.Sprintf("not waiting for auth otp: %s", firstNonEmpty(stage, "idle")), Stage: firstNonEmpty(stage, "idle"), StateJson: stateJSON(state)}, nil
	}
}

func (s *Server) CheckTokenValid(ctx context.Context, req *pb.CheckTokenValidRequest) (*pb.CheckTokenValidResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.checkTokenValid(ctx, state)
	return &pb.CheckTokenValidResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), Stage: firstNonEmpty(stateString(state, "stage"), "idle"), Phone: stateString(state, "phone"), TokenValid: anyBool(result["token_valid"]), Refreshed: anyBool(result["refreshed"]), BalanceAmount: anyInt(result["balance_amount"]), HasMinBalance: anyBool(result["has_min_balance"]), BalanceCurrency: anyString(result["balance_currency"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) ChangePhoneStart(ctx context.Context, req *pb.ChangePhoneStartRequest) (*pb.ChangePhoneStartResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.changePhoneStart(ctx, state, req.GetPin(), req.GetNewPhone(), req.GetCountryCode())
	return &pb.ChangePhoneStartResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), NewPhone: anyString(result["new_phone"]), OtpSent: anyBool(result["otp_sent"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) ChangePhoneRetry(ctx context.Context, req *pb.ChangePhoneRetryRequest) (*pb.ChangePhoneRetryResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.changePhoneRetry(ctx, state)
	return &pb.ChangePhoneRetryResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), OtpSent: anyBool(result["otp_sent"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) ChangePhoneComplete(ctx context.Context, req *pb.ChangePhoneCompleteRequest) (*pb.ChangePhoneCompleteResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.changePhoneComplete(ctx, state, req.GetOtp())
	return &pb.ChangePhoneCompleteResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) DeactivateStart(ctx context.Context, req *pb.DeactivateStartRequest) (*pb.DeactivateStartResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.deactivateStart(ctx, state, req.GetPin())
	return &pb.DeactivateStartResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), OtpSent: anyBool(result["otp_sent"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) DeactivateComplete(ctx context.Context, req *pb.DeactivateCompleteRequest) (*pb.DeactivateCompleteResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.deactivateComplete(ctx, state, req.GetOtp())
	return &pb.DeactivateCompleteResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), DeactivatedAt: anyInt(result["deactivated_at"]), StateJson: stateJSON(state)}, nil
}
