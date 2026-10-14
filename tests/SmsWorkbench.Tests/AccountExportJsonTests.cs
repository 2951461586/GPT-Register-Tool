using System.Collections.Generic;
using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    public class AccountExportJsonTests
    {
        private static Dictionary<string, object> Map(params (string Key, object Value)[] pairs)
        {
            var map = new Dictionary<string, object>(System.StringComparer.OrdinalIgnoreCase);
            foreach (var (key, value) in pairs) map[key] = value;
            return map;
        }

        [Fact]
        public void Clone_DeepCopiesNestedMapsAndLists()
        {
            var inner = Map(("k", "v"));
            var source = Map(("inner", inner), ("list", new List<object> { Map(("a", "1")) }));

            var copy = Assert.IsType<Dictionary<string, object>>(AccountExportJson.Clone(source));
            ((Dictionary<string, object>)copy["inner"])["k"] = "changed";

            Assert.Equal("v", inner["k"]);
            Assert.NotSame(source["list"], copy["list"]);
        }

        [Fact]
        public void EnsureEmail_FillsMissingUserEmailOnly()
        {
            var item = Map();
            AccountExportJson.EnsureEmail(item, "  a@example.com ");
            Assert.Equal("a@example.com", ((Dictionary<string, object>)item["user"])["email"]);

            AccountExportJson.EnsureEmail(item, "b@example.com");
            Assert.Equal("a@example.com", ((Dictionary<string, object>)item["user"])["email"]);
        }

        [Theory]
        [InlineData(null)]
        [InlineData("   ")]
        public void EnsureEmail_IgnoresBlankIdentifier(string? identifier)
        {
            var item = Map();
            AccountExportJson.EnsureEmail(item, identifier);
            Assert.False(item.ContainsKey("user"));
        }

        [Fact]
        public void EnsureRefreshToken_PrefersTopLevelOauthToken()
        {
            var item = Map();
            var source = Map(
                ("oauth_refresh_token", "rt-oauth"),
                ("refresh_token", "rt-plain"));

            AccountExportJson.EnsureRefreshToken(item, source);

            Assert.Equal("rt-oauth", item["refresh_token"]);
            Assert.Equal("rt-oauth", item["oauth_refresh_token"]);
        }

        [Fact]
        public void EnsureRefreshToken_FallsBackToNestedAndKeepsEmptyWhenAbsent()
        {
            var nested = Map();
            AccountExportJson.EnsureRefreshToken(nested, Map(("token", Map(("refresh_token", "rt-nested")))));
            Assert.Equal("rt-nested", nested["refresh_token"]);

            var none = Map();
            AccountExportJson.EnsureRefreshToken(none, Map());
            Assert.Equal("", none["refresh_token"]);
            Assert.False(none.ContainsKey("oauth_refresh_token"));
        }

        [Fact]
        public void FirstString_SkipsBlankAndTrims()
        {
            Assert.Equal("x", AccountExportJson.FirstString(null, "  ", " x ", "y"));
            Assert.Equal("", AccountExportJson.FirstString());
        }

        [Fact]
        public void DedupKey_PrefersUserEmailThenEmailThenIdentifier()
        {
            Assert.Equal("u@example.com", AccountExportJson.DedupKey(
                Map(("user", Map(("email", "U@Example.com"))), ("email", "other@example.com")), "id@example.com"));
            Assert.Equal("top@example.com", AccountExportJson.DedupKey(Map(("email", "Top@Example.com")), "id@example.com"));
            Assert.Equal("id@example.com", AccountExportJson.DedupKey(Map(), " ID@example.com "));
        }

        [Fact]
        public void DedupKey_FallsBackToSerializedItemWhenNoEmailAvailable()
        {
            string key = AccountExportJson.DedupKey(Map(("a", "1")), null);
            Assert.Contains("\"a\"", key);
        }
    }
}
