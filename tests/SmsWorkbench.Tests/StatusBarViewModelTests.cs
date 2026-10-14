using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    public class StatusBarViewModelTests
    {
        [Fact]
        public void Text_StartsReadyAndNotifiesOnChange()
        {
            var bar = new StatusBarViewModel();
            var changed = new System.Collections.Generic.List<string?>();
            bar.PropertyChanged += (_, e) => changed.Add(e.PropertyName);

            Assert.Equal("就绪", bar.Text);

            bar.Text = "批次 1 完成";
            bar.Text = "批次 1 完成";

            Assert.Equal(new[] { "Text" }, changed);
        }

        [Fact]
        public void Text_NullBecomesEmpty()
        {
            var bar = new StatusBarViewModel { Text = null! };

            Assert.Equal("", bar.Text);
        }
    }
}
