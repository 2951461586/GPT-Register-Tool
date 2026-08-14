using System.ComponentModel;

namespace SmsWorkbench.Tests;

public sealed class AccountGridPresentationTests
{
    [Theory]
    [InlineData("可试用Plus·-100%·×1month")]
    [InlineData("可试用 plus")]
    public void TrialEligiblePromotionIsRecognized(string status)
    {
        Assert.True(PromotionStatusPresentation.IsTrialEligible(status));
        Assert.Equal(0, PromotionStatusPresentation.SortRank(status));
    }

    [Fact]
    public void TrialEligiblePromotionUsesSuccessSeverity()
    {
        var converter = new StatusSeverityConverter();

        object severity = converter.Convert(
            "可试用Plus·-100%·×1month",
            typeof(string),
            null!,
            System.Globalization.CultureInfo.InvariantCulture);

        Assert.Equal("success", severity);
    }

    [Fact]
    public void PromotionOrderingRunsBeforePagingAndPrioritizesTrialEligibleRows()
    {
        var rows = new[]
        {
            new PoolRow { Identifier = "empty@example.com", PromotionStatus = "" },
            new PoolRow { Identifier = "none@example.com", PromotionStatus = "无优惠" },
            new PoolRow { Identifier = "trial@example.com", PromotionStatus = "可试用Plus·-100%·×1month" },
        };

        string[] ordered = AccountGridOrdering.Apply(
                rows,
                nameof(PoolRow.PromotionStatus),
                ListSortDirection.Ascending)
            .Select(row => row.Identifier)
            .ToArray();

        Assert.Equal(new[] { "trial@example.com", "none@example.com", "empty@example.com" }, ordered);
    }
}
