namespace SmsWorkbench.Tests;

public sealed class MailBodyFormatterTests
{
    [Fact]
    public void ToDisplayTextRemovesMarkupAndPreservesReadableParagraphs()
    {
        const string html = "<html><head><style>.hidden{display:none}</style></head>"
            + "<body><h1>Verification</h1><p>Your code is <strong>123456</strong>.</p>"
            + "<script>alert('x')</script><div>Expires in 10 minutes.</div></body></html>";

        string result = MailBodyFormatter.ToDisplayText(html);

        Assert.Contains("Verification", result);
        Assert.Contains("Your code is 123456.", result);
        Assert.Contains("Expires in 10 minutes.", result);
        Assert.DoesNotContain("<", result);
        Assert.DoesNotContain("display:none", result);
        Assert.DoesNotContain("alert", result);
    }

    [Fact]
    public void ToDisplayTextDecodesEntitiesAndUsesPreviewFallback()
    {
        string result = MailBodyFormatter.ToDisplayText("", "OpenAI&nbsp;&amp;&nbsp;Codex");

        Assert.Equal("OpenAI & Codex", result);
    }
}
