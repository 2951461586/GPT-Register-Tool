using System.Collections.Generic;
using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    public class RegisterViewModelTests
    {
        [Theory]
        [InlineData("5", 5)]
        [InlineData("0", 1)]
        [InlineData("-3", 1)]
        [InlineData("abc", 1)]
        [InlineData("", 1)]
        public void CountValue_FallsBackToOne(string text, int expected)
        {
            var vm = new RegisterViewModel { CountText = text };

            Assert.Equal(expected, vm.CountValue());
        }

        [Theory]
        [InlineData("3", 3)]
        [InlineData("50", 8)]
        [InlineData("x", 1)]
        public void DefaultWorkerCount_IsCountCappedAtEight(string count, int expected)
        {
            var vm = new RegisterViewModel { CountText = count };

            Assert.Equal(expected, vm.DefaultWorkerCount());
        }

        [Fact]
        public void CountText_NotifiesAndNullBecomesOne()
        {
            var vm = new RegisterViewModel();
            var changed = new List<string?>();
            vm.PropertyChanged += (_, e) => changed.Add(e.PropertyName);

            vm.CountText = "7";
            vm.CountText = null!;

            Assert.Equal("1", vm.CountText);
            Assert.Equal(new[] { "CountText", "CountText" }, changed);
        }

        [Theory]
        [InlineData(" 4 ", 1, 8, 2, 4)]
        [InlineData("99", 1, 8, 2, 8)]
        [InlineData("0", 1, 8, 2, 1)]
        [InlineData("nope", 1, 8, 2, 2)]
        public void ParsePositiveInt_ClampsAndFallsBack(string text, int min, int max, int fallback, int expected)
        {
            Assert.Equal(expected, RegisterViewModel.ParsePositiveInt(text, min, max, fallback));
        }

        [Theory]
        [InlineData("已注册", true)]
        [InlineData("PayPal 待支付", true)]
        [InlineData("支付完成", true)]
        [InlineData("已导入", true)]
        [InlineData("邮箱可用", false)]
        public void HasRegisteredAccountState_MatchesTheKnownStatuses(string status, bool expected)
        {
            Assert.Equal(expected, RegisterViewModel.HasRegisteredAccountState(new PoolRow { Status = status }));
        }

        [Fact]
        public void LocalUnregisteredVerdict_RegisteredOrMissingRowsAreNotPending()
        {
            Assert.False(RegisterViewModel.LocalUnregisteredVerdict(null, false));
            Assert.False(RegisterViewModel.LocalUnregisteredVerdict(
                new PoolRow { Status = "已注册", MailboxLine = "x" }, true));
        }

        [Fact]
        public void LocalUnregisteredVerdict_AnyLocalMailboxEvidenceMeansPending()
        {
            Assert.True(RegisterViewModel.LocalUnregisteredVerdict(new PoolRow(), isCfWorker: true));
            Assert.True(RegisterViewModel.LocalUnregisteredVerdict(new PoolRow { MailboxLine = "line" }, false));
            Assert.True(RegisterViewModel.LocalUnregisteredVerdict(new PoolRow { RawRefreshToken = "rt" }, false));
        }

        [Fact]
        public void LocalUnregisteredVerdict_WithNoLocalEvidenceDefersToTheBackend()
        {
            Assert.Null(RegisterViewModel.LocalUnregisteredVerdict(new PoolRow { Identifier = "a@x.com" }, false));
        }

        [Fact]
        public void MailboxArgument_UsesTheLegacyParserForMixedBatches()
        {
            Assert.Equal("--only", RegisterViewModel.MailboxArgument(new HashSet<string> { "--only" }));
            Assert.Equal(
                "--chatai-mailbox-file",
                RegisterViewModel.MailboxArgument(new HashSet<string> { "--a", "--b" }));
        }
    }
}
