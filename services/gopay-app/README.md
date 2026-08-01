# gopay-app

This directory holds the GoPay App gRPC contract used by GoPay registration and
phone-change flows.

The upstream byte-v-forge implementation runs app auth, signup, PIN setup, and
change-phone flows as a separate provider service. This project does not embed
the upstream Temporal/orchestrator stack. Instead, `services/gopay-flow/gopay.py`
calls the compatible app service through `grpcurl` and returns the structured
state to the CLI payment seam.

Required RPCs for the adapted WA rebind path:

- `GetGoPayState`
- `UpsertGoPayState`
- `AuthStart`
- `AuthComplete`
- `ChangePhoneStart`
- `ChangePhoneComplete`

Default config:

```json
{
  "gopay": {
    "one_click_mode": "wa_rebind",
    "wa_rebind": {
      "enabled": true,
      "gopay_app_service_addr": "127.0.0.1:50060",
      "gopay_app_service": "gopay_app.GopayAppService",
      "gopay_app_proto_import_path": "services\\gopay-app\\proto",
      "gopay_app_proto_path": "services\\gopay-app\\proto\\gopay_app.proto",
      "user_id": "local",
      "wa_phone": "",
      "rebind_phone": ""
    }
  }
}
```

WA payment itself still goes through `services/gopay-flow` on
`gopay.payment_service_addr`.
