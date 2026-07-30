namespace SmsWorkbench
{
    public static class AccessTokenState
    {
        public static string Display(bool hasAccessToken, string probeStatusCode)
        {
            if (!hasAccessToken) return "未获取";
            return string.Equals((probeStatusCode ?? "").Trim(), "401", System.StringComparison.OrdinalIgnoreCase)
                ? "401失效"
                : "已获取";
        }
    }
}
