using System;
using SmsWorkbench;
using Xunit;

namespace SmsWorkbench.Tests
{
    public class LogViewModelTests
    {
        [Fact]
        public void Append_AccumulatesWithoutRaisingPropertyChanged()
        {
            var log = new LogViewModel();
            int notifications = 0;
            log.PropertyChanged += (_, _) => notifications++;

            Assert.False(log.Append("a\n"));
            Assert.False(log.Append("b\n"));

            Assert.Equal("a\nb\n", log.Text);
            Assert.Equal(0, notifications);
        }

        [Fact]
        public void Append_DropsTheOldestTextOnceTheCapIsExceeded()
        {
            var log = new LogViewModel();
            string chunk = new string('x', LogViewModel.MaxBufferChars);
            log.Append(chunk);

            bool trimmed = log.Append("tail");

            Assert.True(trimmed);
            Assert.Equal(LogViewModel.MaxBufferChars, log.Text.Length);
            Assert.EndsWith("tail", log.Text);
        }

        [Fact]
        public void Clear_EmptiesTheBufferAndNotifiesTheBinding()
        {
            var log = new LogViewModel();
            log.Append("line\n");
            var changed = new System.Collections.Generic.List<string?>();
            log.PropertyChanged += (_, e) => changed.Add(e.PropertyName);

            log.Clear();

            Assert.Equal("", log.Text);
            Assert.Equal(new[] { "Text" }, changed);
        }

        [Fact]
        public void ClearCommand_EmptiesTheBufferAndRaisesCleared()
        {
            var log = new LogViewModel();
            log.Append("line\n");
            int cleared = 0;
            log.Cleared += (_, _) => cleared++;

            log.ClearCommand.Execute(null);

            Assert.Equal("", log.Text);
            Assert.Equal(1, cleared);
        }

        [Fact]
        public void FormatLine_PrefixesTheClockTimeAndEndsWithANewline()
        {
            string line = LogViewModel.FormatLine(new DateTime(2026, 10, 14, 9, 5, 7), "hello");

            Assert.Equal("[09:05:07] hello" + Environment.NewLine, line);
        }
    }
}
