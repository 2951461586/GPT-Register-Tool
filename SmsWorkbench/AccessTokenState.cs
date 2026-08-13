namespace SmsWorkbench
{
    public static class AccessTokenState
    {
        public static string ResolveProbeStatusCode(string explicitCode, string accountStatus, string error)
        {
            string code = (explicitCode ?? "").Trim();
            if (code.Length > 0) return code;

            string detail = (error ?? "").Trim();
            if (detail.Contains("401", System.StringComparison.OrdinalIgnoreCase)
                || detail.Contains("unauthorized", System.StringComparison.OrdinalIgnoreCase)
                || detail.Contains("authentication token has been invalidated", System.StringComparison.OrdinalIgnoreCase)
                || detail.Contains("token_invalidated", System.StringComparison.OrdinalIgnoreCase)
                || detail.Contains("could not validate your token", System.StringComparison.OrdinalIgnoreCase))
            {
                return "401";
            }
            return "";
        }

        public static string Display(bool hasAccessToken, string probeStatusCode)
        {
            if (!hasAccessToken) return "未获取";
            return string.Equals((probeStatusCode ?? "").Trim(), "401", System.StringComparison.OrdinalIgnoreCase)
                ? "401失效"
                : "已获取";
        }
    }
}
