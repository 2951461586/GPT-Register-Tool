using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using Xunit;

namespace SmsWorkbench.Tests
{
    public sealed class NamespaceArchitectureTests
    {
        private static readonly string[] RoleFolders =
        {
            "Models",
            "Services",
            "ViewModels",
            "Interop",
            "Dialogs",
            "Views",
            "Localization",
        };

        private static readonly string[] NonUiRoleFolders =
        {
            "Models",
            "Services",
            "ViewModels",
            "Interop",
        };

        private static readonly string[] ForbiddenUiWindowAndControlTypes =
        {
            "MainWindow",
            "SettingsWindow",
            "PaymentBatchWindow",
            "ProxyPoolWindow",
            "AccountFilterBar",
            "AccountGridView",
            "Sidebar",
            "TitleBar",
            "WorkbenchConsole",
            "DialogFactory",
            "InfoConfirmDialogWindow",
            "RegisterOptionsDialog",
            "SelectedRegisterOptionsDialog",
            "ScanOptionsDialog",
            "ExportCompleteDialog",
            "AccountScanResultDialog",
            "ImportTargetDialog",
            "InboxDialogWindow",
            "ChangeEmailDialog",
            "ShellSettingsDialog",
            "AccountBatchProgressWindow",
            "AccountDetailDialog",
            "ExportFormatDialog",
            "SmsProviderDialog",
        };

        [Fact]
        public void EveryRoleFolderCSharpFileDeclaresMatchingRoleNamespace()
        {
            string projectRoot = Path.Combine(RepoFile.Root(), "SmsWorkbench");
            var violations = new List<string>();

            foreach (string folder in RoleFolders)
            {
                string folderPath = Path.Combine(projectRoot, folder);
                string expectedNamespace = $"namespace SmsWorkbench.{folder}";
                foreach (string file in Directory.GetFiles(folderPath, "*.cs", SearchOption.TopDirectoryOnly))
                {
                    string text = File.ReadAllText(file);
                    if (!Regex.IsMatch(text, $@"^\s*{Regex.Escape(expectedNamespace)}\b", RegexOptions.Multiline))
                    {
                        violations.Add($"{folder}/{Path.GetFileName(file)} must declare '{expectedNamespace}'");
                    }
                }
            }

            Assert.Empty(violations);
        }

        [Fact]
        public void EveryWindowAndControlXamlDeclaresMatchingRoleNamespaceClass()
        {
            string projectRoot = Path.Combine(RepoFile.Root(), "SmsWorkbench");
            var violations = new List<string>();

            foreach (string folder in new[] { "Views", "Dialogs" })
            {
                string folderPath = Path.Combine(projectRoot, folder);
                string expectedPrefix = $"x:Class=\"SmsWorkbench.{folder}.";
                foreach (string file in Directory.GetFiles(folderPath, "*.xaml", SearchOption.TopDirectoryOnly))
                {
                    string text = File.ReadAllText(file);
                    if (!text.Contains(expectedPrefix, StringComparison.Ordinal))
                    {
                        violations.Add($"{folder}/{Path.GetFileName(file)} must declare {expectedPrefix}<ClassName>\"");
                    }
                }
            }

            Assert.Empty(violations);
        }

        [Fact]
        public void NonUiLayersDoNotReferenceConcreteWindowsControlsOrDialogs()
        {
            string projectRoot = Path.Combine(RepoFile.Root(), "SmsWorkbench");
            var violations = new List<string>();
            var forbiddenPattern = new Regex(
                $@"\b({string.Join("|", ForbiddenUiWindowAndControlTypes.Select(Regex.Escape))})\b",
                RegexOptions.Compiled);

            foreach (string folder in NonUiRoleFolders)
            {
                string folderPath = Path.Combine(projectRoot, folder);
                foreach (string file in Directory.GetFiles(folderPath, "*.cs", SearchOption.TopDirectoryOnly))
                {
                    string raw = File.ReadAllText(file);
                    string codeOnly = StripComments(raw);
                    Match match = forbiddenPattern.Match(codeOnly);
                    if (match.Success)
                    {
                        violations.Add($"{folder}/{Path.GetFileName(file)} references UI window/control/dialog type '{match.Value}'");
                    }
                }
            }

            Assert.Empty(violations);
        }

        [Fact]
        public void EveryViewModelInheritsCommunityToolkitObservableObject()
        {
            var assembly = typeof(ShellViewModel).Assembly;
            var viewModelTypes = assembly.GetTypes()
                .Where(type => type.Namespace == "SmsWorkbench.ViewModels"
                    && type.IsClass
                    && !type.IsAbstract
                    && type.Name.EndsWith("ViewModel", StringComparison.Ordinal))
                .ToArray();

            Assert.NotEmpty(viewModelTypes);
            foreach (Type type in viewModelTypes)
            {
                Assert.True(
                    typeof(CommunityToolkit.Mvvm.ComponentModel.ObservableObject).IsAssignableFrom(type),
                    $"{type.FullName} must inherit CommunityToolkit.Mvvm.ComponentModel.ObservableObject");
            }
        }

        [Fact]
        public void PaymentProxyRegionPlannerBucketsAndFiltersMixedRegionPools()
        {
            var buckets = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
            var order = new List<string>();
            string rawPool = string.Join("\n", new[]
            {
                "http://user_custom_zone_US:pass@1.2.3.4:8080",
                "http://user_region-jp:pass@5.6.7.8:8080",
                "http://plainuser:pass@9.9.9.9:8080",
            });

            PaymentProxyRegionPlanner.InitializeBuckets(rawPool, buckets, order);
            Assert.Equal("US", PaymentProxyRegionPlanner.InferProxyRegion("http://user_custom_zone_US:pass@1.2.3.4:8080"));
            Assert.Equal("JP", PaymentProxyRegionPlanner.InferProxyRegion("http://user_region-jp:pass@5.6.7.8:8080"));
            Assert.Equal("", PaymentProxyRegionPlanner.InferProxyRegion("http://plainuser:pass@9.9.9.9:8080"));

            string usDisplay = PaymentProxyRegionPlanner.ComputeDisplay("US", buckets, order);
            Assert.Contains("custom_zone_US", usDisplay, StringComparison.Ordinal);
            Assert.Contains("plainuser", usDisplay, StringComparison.Ordinal);
            Assert.DoesNotContain("region-jp", usDisplay, StringComparison.Ordinal);
        }

        [Fact]
        public void PaymentBatchReportParserParsesProbeAndResults()
        {
            using var doc = System.Text.Json.JsonDocument.Parse("""
                {
                  "ok": true,
                  "stages": {
                    "checkout": { "ok": true, "country_code": "US", "region": "CA", "ip": "1.1.1.1" }
                  },
                  "results": [
                    {
                      "account_ref": "a@example.com",
                      "authenticated": true,
                      "refreshed": true,
                      "eligible": true,
                      "ok": true,
                      "terminal_state": "completed",
                      "url": "https://pay.example.com/1",
                      "attempts": 1
                    }
                  ]
                }
                """);
            string probeText = PaymentBatchReportParser.FormatProxyProbe(doc.RootElement);
            Assert.Contains("checkout✓ US/CA 1.1.1.1", probeText, StringComparison.Ordinal);

            var rows = PaymentBatchReportParser.ParseResults(doc.RootElement, id => id);
            Assert.Single(rows);
            Assert.Equal("a@example.com", rows[0].AccountRef);
            Assert.Equal("200", rows[0].AuthStatus);
            Assert.True(rows[0].ResultPresent);
        }

        private static string StripComments(string source)
        {
            string withoutBlock = Regex.Replace(source, @"/\*.*?\*/", string.Empty, RegexOptions.Singleline);
            return Regex.Replace(withoutBlock, @"//.*", string.Empty);
        }
    }
}
