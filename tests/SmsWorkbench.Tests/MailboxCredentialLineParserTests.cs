using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    /// <summary>
    /// 邮箱凭据行解析（placement rule 8：解析必须 window-independent）。
    /// 用例从 MainWindow.Export 的原私有实现语义逐条迁移，防回归。
    /// </summary>
    public class MailboxCredentialLineParserTests
    {
        private static bool TryParse(string source, out string email, out string password, out string clientId, out string refreshToken)
        {
            int factoryCalls = 0;
            return MailboxCredentialLineParser.TryParseMailboxExportParts(
                source,
                () => { factoryCalls++; return "fallback-client-id"; },
                out email, out password, out clientId, out refreshToken);
        }

        [Theory]
        [InlineData(null)]
        [InlineData("")]
        [InlineData("# comment line")]
        [InlineData("cfworker://abc")]
        [InlineData("user@edu.liziai.cloud")]
        [InlineData("user@liziai.cloud")]
        [InlineData("user@example.com----pw")]
        public void RejectsNonCredentialLines(string? source)
        {
            Assert.False(TryParse(source!, out _, out _, out _, out _));
        }

        [Fact]
        public void ParsesFourSegmentLineWithGuidClientId()
        {
            Assert.True(TryParse(
                "user@example.com----pw123----0123abcd-0000-1111-2222-333344445555----rt-token",
                out string email, out string password, out string clientId, out string refreshToken));
            Assert.Equal("user@example.com", email);
            Assert.Equal("pw123", password);
            Assert.Equal("0123abcd-0000-1111-2222-333344445555", clientId);
            Assert.Equal("rt-token", refreshToken);
        }

        [Fact]
        public void FourSegmentSwapsClientAndRefreshWhenGuidIsInTheThirdPart()
        {
            Assert.True(TryParse(
                "user@example.com----pw123----not-a-guid----0123abcd-0000-1111-2222-333344445555",
                out _, out _, out string clientId, out string refreshToken));
            Assert.Equal("0123abcd-0000-1111-2222-333344445555", clientId);
            Assert.Equal("not-a-guid", refreshToken);
        }

        [Fact]
        public void FourSegmentWithoutAnyGuidKeepsGivenOrder()
        {
            Assert.True(TryParse(
                "user@example.com----pw123----part2----part3",
                out _, out _, out string clientId, out string refreshToken));
            Assert.Equal("part2", clientId);
            Assert.Equal("part3", refreshToken);
        }

        [Fact]
        public void FourSegmentRejoinsExtraSeparatorsIntoRefreshToken()
        {
            Assert.True(TryParse(
                "user@example.com----pw----0123abcd-0000-1111-2222-333344445555----rt----extra",
                out _, out _, out _, out string refreshToken));
            Assert.Equal("rt----extra", refreshToken);
        }

        [Fact]
        public void ThreeSegmentLineUsesTheFallbackClientId()
        {
            Assert.True(TryParse(
                "user@example.com---pw123---rt-token",
                out string email, out string password, out string clientId, out string refreshToken));
            Assert.Equal("user@example.com", email);
            Assert.Equal("pw123", password);
            Assert.Equal("fallback-client-id", clientId);
            Assert.Equal("rt-token", refreshToken);
        }

        [Fact]
        public void TrimsBomAndWhitespace()
        {
            Assert.True(TryParse(
                "\ufeff  user@example.com----pw123----0123abcd-0000-1111-2222-333344445555----rt  ",
                out string email, out string password, out _, out string refreshToken));
            Assert.Equal("user@example.com", email);
            Assert.Equal("pw123", password);
            Assert.Equal("rt", refreshToken);
        }

        [Fact]
        public void LooksMicrosoftClientIdRequiresTheExactGuidShape()
        {
            Assert.True(MailboxCredentialLineParser.LooksMicrosoftClientId("0123abcd-0000-1111-2222-333344445555"));
            Assert.True(MailboxCredentialLineParser.LooksMicrosoftClientId("  0123ABCD-0000-1111-2222-333344445555 "));
            Assert.False(MailboxCredentialLineParser.LooksMicrosoftClientId("not-a-guid"));
            Assert.False(MailboxCredentialLineParser.LooksMicrosoftClientId("0123abcd-0000-1111-2222-3333444455"));
        }
    }
}
