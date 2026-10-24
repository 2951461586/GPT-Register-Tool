using Xunit;

// The desktop tests are not safe to run in parallel test *collections*:
//
// * `LocalizationTests` switches the process-wide `Loc.Instance` language, while
//   several view-model / presentation tests assert the Chinese source string;
// * the window smoke tests own the WPF `Application`, theme manager and an STA
//   thread, which are single-instance by construction.
//
// Measured 2026-10-24: with xUnit's default parallel collections, the same 548
// tests intermittently failed in different classes (`ExportViewModelTests`,
// `RegistrationStatusPresentationTests`, `SettingsServiceTests`) — always a
// language-dependent assertion, never the same one twice. Serialising the
// collections makes the suite deterministic and costs a few seconds.
[assembly: CollectionBehavior(DisableTestParallelization = true)]
