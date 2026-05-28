# gopay-flow

`gopay-flow` is the project-local gRPC service wrapper for the GoPay protocol payment flow. It is used by `sms_tool.gopay_payment` when `gopay.one_click_mode=provider` or `wa_rebind`.

## Service API

Proto: `proto/payment.proto`

```proto
service PaymentService {
  rpc PrepareGoPayCheckout(PrepareGoPayCheckoutRequest) returns (PrepareGoPayResponse);
  rpc RefreshPrepareGoPayCheckout(RefreshPrepareGoPayCheckoutRequest) returns (PrepareGoPayResponse);
  rpc PrepareGoPayLink(PrepareGoPayLinkRequest) returns (PrepareGoPayResponse);
  rpc PrepareGoPay(PrepareGoPayRequest) returns (PrepareGoPayResponse);
  rpc StartPreparedGoPay(StartPreparedGoPayRequest) returns (StartGoPayResponse);
  rpc StartGoPay(StartGoPayRequest) returns (StartGoPayResponse);
  rpc CreateCheckoutLink(CreateCheckoutLinkRequest) returns (CreateCheckoutLinkResponse);
  rpc ProbePlusTrial(ProbePlusTrialPaymentRequest) returns (ProbePlusTrialPaymentResponse);
  rpc ProbeTier(ProbeTierPaymentRequest) returns (ProbeTierPaymentResponse);
  rpc CompleteGoPay(CompleteGoPayRequest) returns (GoPayResponse);
  rpc ResendGoPayOTP(ResendGoPayOTPRequest) returns (ResendGoPayOTPResponse);
  rpc ConfirmGoPayPayment(ConfirmGoPayPaymentRequest) returns (GoPayResponse);
  rpc CancelGoPay(CancelGoPayRequest) returns (CancelGoPayResponse);
}
```

Flow:

1. `StartGoPay` creates checkout, runs Stripe/Midtrans/GoPay linking, triggers OTP, and returns `flow_id`.
2. An external orchestrator waits for OTP.
3. `CompleteGoPay` submits OTP, PIN, charge, and ChatGPT verify.
4. If configured, successful completion triggers GoPay unlink.
5. `CancelGoPay` closes a pending flow.

WA rebind mode uses the same payment service with `otp_channel=wa`. After the payment completes, `sms_tool.gopay_wa_rebind` calls the separate GoPay App service contract in `services/gopay-app/proto/gopay_app.proto` for app auth and `ChangePhoneStart/ChangePhoneComplete`.

## Run

```powershell
powershell -ExecutionPolicy Bypass -File ..\..\scripts\start_gopay_provider.ps1
```

## ADB Sidecar

For the current LDPlayer setup, start the host-side sidecar from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File adb\start_ldplayer_sidecar.ps1
```

Config points OTP and unlink to:

```json
{
  "gopay": {
    "otp": {"source": "adb", "adb_url": "http://127.0.0.1:9999"},
    "unlink": {"enabled": true, "adb_url": "http://127.0.0.1:9999"}
  }
}
```

Probe the local PaymentService:

```powershell
grpcurl -plaintext -import-path services\gopay-flow\proto -proto payment.proto `
  127.0.0.1:50051 list payment.PaymentService
```

## Notes

- `payment_server.py` is the maintained local provider path for this repository.
- The ADB sidecar is still used for local OTP notification polling and unlink.
- WA rebind requires a compatible `gopay_app.GopayAppService`; this repository stores the proto contract and the adapted caller, not the upstream Temporal workflow.
