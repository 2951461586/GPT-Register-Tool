namespace SmsWorkbench.Models
{
    public sealed partial class PoolRow : ObservableObject
    {
        [ObservableProperty] private bool isChecked;
        public string Id { get; set; } = "";
        public string CreatedAt { get; set; } = "";
        public string CompletedAt { get; set; } = "";
        public string Identifier { get; set; } = "";
        public string AccountType { get; set; } = "";
        public string AccountPlanType { get; set; } = "";
        public string Source { get; set; } = "";
        public string RegisterMethod { get; set; } = "";
        public string SessionType { get; set; } = "";
        public string PlanType { get; set; } = "";
        public string RegistrationCountry { get; set; } = "";
        // Provenance from the backend's `identity` block: the exit this account was
        // registered through and the fingerprint it was bound to. Both are already
        // persisted per account (`identity_context` in the session JSON); before
        // this the detail panel had no way to show them, so the one property that
        // distinguishes two accounts was invisible in the UI that manages them.
        public string FingerprintProfile { get; set; } = "";
        public string EgressEndpoint { get; set; } = "";
        public string EgressCountry { get; set; } = "";
        public bool IdentityBound { get; set; }
        public string RegistrationStatus { get; set; } = "";
        public string Status { get; set; } = "";
        public string PayPalStatus { get; set; } = "";
        public string PayPalAmount { get; set; } = "";
        public string PromotionStatus { get; set; } = "";
        // Machine promotion state from the desktop read payload
        // (sms_tool/payment/promotion_states.py); the label above is display copy.
        public string PromotionState { get; set; } = "";
        // 支付资格 (sms_tool/payment/qualification_states.py): the label is display
        // copy, the state is what filters/sorts key off.
        public string PaymentQualification { get; set; } = "";
        public string PaymentQualificationState { get; set; } = "";
        public string RefreshTokenStatus { get; set; } = "";
        /// <summary>Machine fact (<c>totp_present</c>/<c>totp_enrolled</c>/<c>has_totp</c>).</summary>
        public bool TwoFactorEnabled { get; set; }
        public string TwoFactorStatus { get; set; } = "未设置";
        public string Phone { get; set; } = "";
        public bool HasAccessToken { get; set; }
        public string AccessTokenProbeStatusCode { get; set; } = "";
        public string AccessTokenStatus => AccessTokenState.Display(HasAccessToken, AccessTokenProbeStatusCode);
        public string PayPalUrl { get; set; } = "";
        public string RefreshToken { get; set; } = "";
        public string Proxy { get; set; } = "";
        public string Notes { get; set; } = "";
        public string SourcePath { get; set; } = "";
        public string RawLine { get; set; } = "";
        public string MailboxLine { get; set; } = "";
        public string ClientId { get; set; } = "";
        public string RawRefreshToken { get; set; } = "";
        public string MailboxProvider { get; set; } = "";
        public string MailboxToken { get; set; } = "";

        // Badge severity, derived from machine state (AccountMachineState). Bound
        // directly instead of routing the *display* label through a converter:
        // the label is prose, so a reword or a language switch used to recolour
        // the grid. See docs/audits/plans/plan-2026-10-25-wpf-row-status-decoupling.md.
        public string StatusSeverity => AccountMachineState.StatusSeverity(this);
        public string AccessTokenSeverity => AccountMachineState.AccessTokenSeverity(this);
        public string RefreshTokenSeverity => AccountMachineState.RefreshTokenSeverity(this);
        public string TwoFactorSeverity => AccountMachineState.TwoFactorSeverity(this);
        public string PromotionSeverity => AccountMachineState.PromotionSeverity(this);
    }
}
