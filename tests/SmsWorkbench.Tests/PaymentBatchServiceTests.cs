using System.Text.Json;
using SmsWorkbench;

namespace SmsWorkbench.Tests;

public sealed class PaymentBatchServiceTests
{
    [Fact]
    public async Task ProbeModeUsesContractArgumentsAndAcceptsPayloadFromNonZeroExit()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        string emailFile = "";
        string matrixFile = "";
        var backend = new StubBackendClient
        {
            Handler = command =>
            {
                emailFile = ArgumentAfter(command.Arguments, "--email-file");
                matrixFile = ArgumentAfter(command.Arguments, "--payment-matrix");
                Assert.True(File.Exists(emailFile));
                Assert.True(File.Exists(matrixFile));
                return new BackendCommandResult(
                    3,
                    "",
                    "backend classified the run",
                    JsonElementOf("{\"ok\":false,\"error\":\"classified\"}"),
                    false);
            }
        };
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), backend);
        var request = new PaymentBatchRequest(
            new[] { new PaymentBatchAccount("first@example.com", true) },
            "momo",
            2,
            1,
            0,
            "probe-batch",
            "http://proxy.example:8080",
            true,
            true,
            true,
            new[] { service.CreateDefaultMatrixRow("momo") });

        JsonElement payload = await service.RunAsync(request, CancellationToken.None);

        Assert.False(payload.GetProperty("ok").GetBoolean());
        Assert.NotNull(backend.LastCommand);
        Assert.Contains("--desktop-ipc", backend.LastCommand.Arguments);
        Assert.Contains("--extract-payment-link", backend.LastCommand.Arguments);
        Assert.Equal("momo", ArgumentAfter(backend.LastCommand.Arguments, "--payment-method"));
        Assert.Contains("--payment-probe-only", backend.LastCommand.Arguments);
        Assert.DoesNotContain("--no-require-zero", backend.LastCommand.Arguments);
        Assert.Equal("probe-batch", ArgumentAfter(backend.LastCommand.Arguments, "--payment-batch-id"));
        Assert.False(File.Exists(emailFile));
        Assert.False(File.Exists(matrixFile));
    }

    [Fact]
    public async Task FormalModeSerializesValidatedMatrixAndOptions()
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        string matrixJson = "";
        var backend = new StubBackendClient
        {
            Handler = command =>
            {
                matrixJson = File.ReadAllText(ArgumentAfter(command.Arguments, "--payment-matrix"));
                return new BackendCommandResult(0, "", "", JsonElementOf("{\"ok\":true}"), false);
            }
        };
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), backend);
        var matrix = new PaymentMatrixRow
        {
            Name = "vn-primary",
            RegistrationCountry = "vn",
            CheckoutCountry = "jp",
            SampleSize = 3
        };
        var request = new PaymentBatchRequest(
            new[] { new PaymentBatchAccount("first@example.com", false) },
            "momo",
            1,
            0,
            1,
            "formal-batch",
            "",
            false,
            false,
            false,
            new[] { matrix });

        await service.RunAsync(request, CancellationToken.None);

        Assert.NotNull(backend.LastCommand);
        Assert.DoesNotContain("--payment-probe-only", backend.LastCommand.Arguments);
        Assert.Contains("--no-jit-at-refresh", backend.LastCommand.Arguments);
        Assert.Contains("--no-require-zero", backend.LastCommand.Arguments);
        Assert.Equal("1", ArgumentAfter(backend.LastCommand.Arguments, "--payment-canary"));
        using JsonDocument document = JsonDocument.Parse(matrixJson);
        JsonElement cell = document.RootElement.GetProperty("cells")[0];
        Assert.Equal("VN", cell.GetProperty("registration_country").GetString());
        Assert.Equal("JP", cell.GetProperty("checkout_country").GetString());
        Assert.Equal(3, cell.GetProperty("sample_size").GetInt32());
    }

    [Theory]
    [InlineData("gopay", "ID")]
    [InlineData("gcash", "PH")]
    [InlineData("grabpay", "PH")]
    public void WalletDefaultMatrixUsesProviderCountryForEveryStage(
        string paymentMethod,
        string expectedCountry)
    {
        using var fixture = new TemporaryDirectory();
        File.WriteAllText(Path.Combine(fixture.Path, "config.json"), "{}");
        var service = new PaymentBatchService(new TestApplicationPaths(fixture.Path), new StubBackendClient());

        PaymentMatrixRow row = service.CreateDefaultMatrixRow(paymentMethod);

        Assert.Equal(expectedCountry.ToLowerInvariant() + "_" + paymentMethod, row.Name);
        Assert.Equal(expectedCountry, row.RegistrationCountry);
        Assert.Equal(expectedCountry, row.CheckoutCountry);
        Assert.Equal(expectedCountry, row.PromotionCountry);
        Assert.Equal(expectedCountry, row.ProviderCountry);
        Assert.Equal(expectedCountry, row.ApproveCountry);
        Assert.Equal(expectedCountry, row.RedirectCountry);
        Assert.Equal(1, row.SampleSize);
    }

    private static string ArgumentAfter(IReadOnlyList<string> arguments, string option)
    {
        int index = arguments.ToList().IndexOf(option);
        Assert.True(index >= 0 && index + 1 < arguments.Count, $"Missing argument {option}");
        return arguments[index + 1];
    }

    private static JsonElement JsonElementOf(string json)
    {
        using JsonDocument document = JsonDocument.Parse(json);
        return document.RootElement.Clone();
    }
}

internal sealed class TemporaryDirectory : IDisposable
{
    public TemporaryDirectory()
    {
        Path = System.IO.Path.Combine(
            System.IO.Path.GetTempPath(),
            "smsworkbench-tests-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path);
    }

    public string Path { get; }

    public void Dispose()
    {
        if (Directory.Exists(Path))
            Directory.Delete(Path, true);
    }
}
