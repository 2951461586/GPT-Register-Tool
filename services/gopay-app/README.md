# gopay-app

This directory holds the GoPay App gRPC contract used by WA rebind mode.

The upstream byte-v-forge implementation runs app auth, signup, PIN setup, and
change-phone flows as a separate provider service. This project does not embed
the upstream Temporal/orchestrator stack. Instead, `sms_tool.gopay_wa_rebind`
calls the compatible app service through `grpcurl` and persists the returned
`state_json` in the local session/SQLite record.

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
