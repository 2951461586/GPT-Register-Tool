using System.IO;
using System.Text.Json;
using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    /// <summary>
    /// 跨语言优惠状态契约：与 tests/test_account_promotion.py 共用
    /// tests/fixtures/promotion_status_cases.json。桌面过滤/排序消费机器状态
    /// （promotion_state），展示文案只是 UI 文本 —— 任一侧漂移即失败。
    /// </summary>
    public class PromotionStatusContractTests
    {
        private static JsonElement Fixture()
        {
            string path = Path.Combine(AppContext.BaseDirectory, "promotion_status_cases.json");
            return JsonDocument.Parse(File.ReadAllText(path)).RootElement;
        }

        [Fact]
        public void MachineStateLiterals_ArePinnedByTheSharedFixture()
        {
            JsonElement contract = Fixture().GetProperty("contract");
            Assert.Equal("trial_eligible", contract.GetProperty("trial_eligible").GetString());
            Assert.Equal(
                PromotionStatusPresentation.TrialEligibleState,
                contract.GetProperty("trial_eligible").GetString());
        }

        [Fact]
        public void TrialEligibility_MatchesTheSharedFixture()
        {
            foreach (JsonElement c in Fixture().GetProperty("cases").EnumerateArray())
            {
                string name = c.GetProperty("name").GetString() ?? "";
                string label = c.GetProperty("label").GetString() ?? "";
                string state = c.GetProperty("state").GetString() ?? "";
                bool legacyFallback = c.GetProperty("legacy_fallback").GetBoolean();
                int sortRank = c.GetProperty("sort_rank").GetInt32();

                // 机器状态存在时绝对胜出；无状态旧行回落到文案子串判定。
                Assert.True(
                    PromotionStatusPresentation.IsTrialEligible(label, state) == legacyFallback,
                    name);
                Assert.True(
                    PromotionStatusPresentation.IsTrialEligibleLabel(label) == legacyFallback,
                    name);
                // 机器状态与文案子串在夹具样例上必须一致 —— 出现分歧时让契约
                // 测试失败，而不是让旧行为悄悄改变新行为。
                Assert.True(
                    PromotionStatusPresentation.IsTrialEligible(label) == legacyFallback,
                    name);
                Assert.Equal(sortRank, PromotionStatusPresentation.SortRank(label, state));
            }
        }
    }
}
