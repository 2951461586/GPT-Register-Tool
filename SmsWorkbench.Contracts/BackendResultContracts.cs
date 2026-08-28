namespace SmsWorkbench;

/// <summary>
/// Structured result for a single proxy-test stage.
/// </summary>
public sealed record ProxyTestStageResult(
    string Stage,
    string Ip,
    string ActualCountry,
    string ExpectedCountry,
    string Error);

/// <summary>
/// Structured result for a complete proxy test.
/// </summary>
public sealed record ProxyTestResult(
    bool AllOk,
    IReadOnlyList<ProxyTestStageResult> Stages);

/// <summary>
/// Uniform interpretation of a backend command execution.
/// </summary>
public sealed record BackendExecutionResult(
    bool IsSuccess,
    string DisplayText,
    string State,
    JsonElement? Payload);
