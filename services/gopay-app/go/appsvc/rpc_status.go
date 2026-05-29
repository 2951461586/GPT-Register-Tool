package appsvc

import (
	"context"
	"time"

	"github.com/byte-v-forge/gpt/gopay/pb"
)

func (s *Server) Status(ctx context.Context, req *pb.StatusRequest) (*pb.StatusResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	s.expireLoginIfNeeded(state)
	s.expireSignupIfNeeded(state)
	if stateString(state, "stage") == "ready" {
		_ = s.ensureAccessToken(ctx, state, s.cfg.TokenRefreshMinTTL, false)
	}
	errorMessage := stateString(state, "last_error")
	if stateString(state, "last_token_refresh_error") != "" && !tokenUsable(state, "token", 0) {
		errorMessage = stateString(state, "last_token_refresh_error")
	}
	return &pb.StatusResponse{
		Stage:                     firstNonEmpty(stateString(state, "stage"), "idle"),
		Phone:                     stateString(state, "phone"),
		DeviceFingerprint:         deviceFingerprintForState(state),
		DeactivatedAt:             stateInt(state, "deactivated_at"),
		ErrorMessage:              errorMessage,
		TokenPresent:              tokenUsable(state, "token", 30*time.Second),
		LoginOtpSentAtUnix:        stateInt(state, "_login_otp_sent_at"),
		LoginOtpExpiresAtUnix:     stateInt(state, "_login_otp_expires_at"),
		SignupOtpSentAtUnix:       stateInt(state, "_signup_otp_sent_at"),
		SignupOtpExpiresAtUnix:    stateInt(state, "_signup_otp_expires_at"),
		SignupPinOtpSentAtUnix:    stateInt(state, "_signup_pin_otp_sent_at"),
		SignupPinOtpExpiresAtUnix: stateInt(state, "_signup_pin_otp_expires_at"),
		BalanceAmount:             stateInt(state, "balance_amount"),
		HasMinBalance:             anyBool(state["has_min_balance"]),
		BalanceCurrency:           stateString(state, "balance_currency"),
		PinSetup:                  anyBool(state["pin_setup"]) || stateInt(state, "pin_setup_at") > 0,
		PinSetupAtUnix:            stateInt(state, "pin_setup_at"),
		StateJson:                 stateJSON(state),
	}, nil
}

func (s *Server) GetQrId(ctx context.Context, req *pb.GetQrIdRequest) (*pb.GetQrIdResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.getQrID(ctx, state)
	return &pb.GetQrIdResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), QrId: anyString(result["qr_id"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) GetReadyAccountToken(ctx context.Context, req *pb.GetReadyAccountTokenRequest) (*pb.GetReadyAccountTokenResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	if stateString(state, "stage") == "ready" {
		check := s.checkTokenValid(ctx, state)
		if !anyBool(check["success"]) || !anyBool(check["token_valid"]) {
			return &pb.GetReadyAccountTokenResponse{Success: false, ErrorMessage: firstNonEmpty(anyString(check["error"]), "token validation failed"), StateJson: stateJSON(state)}, nil
		}
		if !anyBool(check["has_min_balance"]) {
			return &pb.GetReadyAccountTokenResponse{Success: false, ErrorMessage: s.tokenCheckError(check), StateJson: stateJSON(state)}, nil
		}
	}
	token := stateString(state, "token")
	if stateString(state, "stage") != "ready" || token == "" {
		return &pb.GetReadyAccountTokenResponse{Success: false, ErrorMessage: "account token not ready: stage=" + firstNonEmpty(stateString(state, "stage"), "idle"), StateJson: stateJSON(state)}, nil
	}
	if !tokenUsable(state, "token", 0) {
		return &pb.GetReadyAccountTokenResponse{Success: false, ErrorMessage: "account token expired", StateJson: stateJSON(state)}, nil
	}
	return &pb.GetReadyAccountTokenResponse{Success: true, AccountToken: token, Phone: stateString(state, "phone"), StateJson: stateJSON(state)}, nil
}

func (s *Server) CheckDeactivation(_ context.Context, req *pb.CheckDeactivationRequest) (*pb.CheckDeactivationResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	if stateInt(state, "deactivated_at") <= 0 {
		return &pb.CheckDeactivationResponse{Completed: false, RemainingSeconds: -1, StateJson: stateJSON(state)}, nil
	}
	return &pb.CheckDeactivationResponse{Completed: true, RemainingSeconds: 0, StateJson: stateJSON(state)}, nil
}

func (s *Server) Unlink(ctx context.Context, req *pb.UnlinkRequest) (*pb.UnlinkResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.unlink(ctx, state)
	return &pb.UnlinkResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), UnlinkedCount: int32(anyInt(result["unlinked_count"])), StateJson: stateJSON(state)}, nil
}

func (s *Server) ClaimEnvelope(ctx context.Context, req *pb.ClaimEnvelopeRequest) (*pb.ClaimEnvelopeResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result := s.claimEnvelope(ctx, state, req.GetEnvelopeRequestId(), req.GetLink())
	return &pb.ClaimEnvelopeResponse{Success: anyBool(result["success"]), ErrorMessage: anyString(result["error"]), EnvelopeRequestId: anyString(result["envelope_request_id"]), ResponseEnvelopeRequestId: anyString(result["response_envelope_request_id"]), Status: anyString(result["status"]), HttpStatus: int32(anyInt(result["http_status"])), RawJson: anyString(result["raw_json"]), StateJson: stateJSON(state)}, nil
}

func (s *Server) ReplayLinkPayment(ctx context.Context, req *pb.ReplayLinkPaymentRequest) (*pb.ReplayLinkPaymentResponse, error) {
	state := s.parseRequestState(req.GetStateJson())
	result, err := s.replayLinkPayment(ctx, state, req.GetPaymentLink(), req.GetPin(), req.GetAmountValue(), req.GetAmountCurrency(), int(req.GetBodyLimit()))
	if err != nil {
		return &pb.ReplayLinkPaymentResponse{Success: false, ErrorMessage: err.Error(), StateJson: stateJSON(state)}, nil
	}
	steps := make([]*pb.ReplayStepResult, 0, len(result.Steps))
	for _, step := range result.Steps {
		steps = append(steps, &pb.ReplayStepResult{Label: step.Label, StatusCode: int32(step.StatusCode), ResponseText: step.ResponseText, ErrorMessage: step.ErrorMessage})
	}
	return &pb.ReplayLinkPaymentResponse{Success: result.Success, ErrorMessage: result.ErrorMessage, PaymentId: result.PaymentID, Status: result.Status, Steps: steps, StateJson: stateJSON(state)}, nil
}

func int32Slice(value any) []int32 {
	items, ok := value.([]any)
	if !ok {
		return nil
	}
	out := make([]int32, 0, len(items))
	for _, item := range items {
		out = append(out, int32(anyInt(item)))
	}
	return out
}
