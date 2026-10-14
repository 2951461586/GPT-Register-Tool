using System.Collections.Generic;
using System.Linq;
using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    public class ExportViewModelTests
    {
        private static PoolRow Row(string id, string status = "已注册", bool hasAt = false) =>
            new PoolRow { Identifier = id, Status = status, HasAccessToken = hasAt };

        private static (PoolViewModel Pool, ExportViewModel Export) Create(params PoolRow[] rows)
        {
            var pool = new PoolViewModel();
            foreach (PoolRow row in rows) pool.AllRows.Add(row);
            return (pool, new ExportViewModel(pool));
        }

        [Fact]
        public void CandidateRows_PrefersTheSelection()
        {
            var (_, export) = Create(Row("a@x.com"), Row("b@x.com"));
            var selected = new List<PoolRow> { Row("picked@x.com") };

            Assert.Same(selected, export.CandidateRows(selected));
        }

        [Fact]
        public void CandidateRows_FallsBackToTheFilteredRowsThenTheWholePool()
        {
            var (pool, export) = Create(Row("a@x.com"), Row("b@x.com"), Row("c@x.com"));

            pool.SearchText = "b@x";
            Assert.Equal(new[] { "b@x.com" }, export.CandidateRows(new List<PoolRow>()).Select(r => r.Identifier));

            pool.SearchText = "nomatch";
            Assert.Equal(3, export.CandidateRows(new List<PoolRow>()).Count);
        }

        [Fact]
        public void ImportableRows_DeduplicatesByEmailIgnoringCase()
        {
            var (_, export) = Create();
            var selected = new[] { Row("A@x.com"), Row(" a@x.com "), Row("b@x.com") };

            Assert.Equal(new[] { "A@x.com", "b@x.com" }, export.ImportableRows(selected).Select(r => r.Identifier));
        }

        [Fact]
        public void ImportableRows_FallsBackToThePoolWhenTheSelectionHasNothingImportable()
        {
            var (_, export) = Create(Row("pool@x.com"), Row("skip@x.com", status: "失败"));
            var selected = new[] { Row("nope@x.com", status: "失败") };

            Assert.Equal(new[] { "pool@x.com" }, export.ImportableRows(selected).Select(r => r.Identifier));
        }

        [Theory]
        [InlineData("已注册", false, true)]
        [InlineData("待支付", false, true)]
        [InlineData("失败", true, true)]
        [InlineData("失败", false, false)]
        public void IsImportable_AcceptsRegisteredLikeStatusesOrAnAccessToken(string status, bool hasAt, bool expected)
        {
            Assert.Equal(expected, ExportViewModel.IsImportable(Row("a@x.com", status, hasAt)));
        }

        [Fact]
        public void IsImportable_RejectsRowsWithoutAnEmail()
        {
            Assert.False(ExportViewModel.IsImportable(Row("  ", hasAt: true)));
            Assert.False(ExportViewModel.IsImportable(null));
        }

        [Theory]
        [InlineData("sub2api", "SUB2API JSON")]
        [InlineData(" Codex ", "Codex auth.json")]
        [InlineData("txt", "TXT")]
        [InlineData("json", "原始 JSON")]
        [InlineData("anything-else", "CPA JSON")]
        [InlineData(null, "CPA JSON")]
        public void FormatLabel_MapsKnownFormatsAndDefaultsToCpa(string? format, string expected)
        {
            Assert.Equal(expected, ExportViewModel.FormatLabel(format!));
        }

        [Fact]
        public void FormatDescription_MentionsTheConverterForConvertedFormats()
        {
            Assert.Contains("Sub2API", ExportViewModel.FormatDescription("sub2api"));
            Assert.Contains("CPA JSON", ExportViewModel.FormatDescription("unknown"));
        }

        [Fact]
        public void FormatDirectProbeSummary_ToleratesNullInputs()
        {
            string text = ExportViewModel.FormatDirectProbeSummary(null!, null!);

            Assert.StartsWith("总数：0", text);
            Assert.DoesNotContain("重登", text);
        }
    }
}
