using System.Text.Json;
using Xunit;

namespace SmsWorkbench.Tests
{
    /// <summary>
    /// 跨语言 OAuth client id 契约：与 tests/test_mailbox_graph_pure.py 共用
    /// tests/fixtures/oauth_client_id.json。桌面回落值与 Python 默认值必须是
    /// 同一个 Microsoft Graph OAuth 应用 —— 任一侧单改，另一侧就会静默注册到
    /// 不同的应用上。
    /// </summary>
    public class OAuthClientIdContractTests
    {
        private static string RepoRoot()
        {
            DirectoryInfo? dir = new DirectoryInfo(AppContext.BaseDirectory);
            for (int i = 0; i < 10 && dir != null; i++, dir = dir.Parent)
            {
                if (File.Exists(Path.Combine(dir.FullName, "SmsWorkbench", "MainWindow.Export.cs")))
                    return dir.FullName;
            }
            throw new InvalidOperationException("repo root not found from test output");
        }

        [Fact]
        public void DefaultMailboxClientId_Fallback_MatchesTheSharedFixture()
        {
            string fixturePath = Path.Combine(AppContext.BaseDirectory, "oauth_client_id.json");
            using JsonDocument doc = JsonDocument.Parse(File.ReadAllText(fixturePath));
            string expected = doc.RootElement.GetProperty("client_id").GetString() ?? "";

            string source = File.ReadAllText(
                Path.Combine(RepoRoot(), "SmsWorkbench", "MainWindow.Export.cs"));
            Assert.Contains(expected, source);
        }
    }
}
