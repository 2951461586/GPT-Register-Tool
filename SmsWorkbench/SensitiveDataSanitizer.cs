using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;

namespace SmsWorkbench
{
    internal static class SensitiveDataSanitizer
    {
        private const string PolicyResource = "SmsWorkbench.sensitive_policy.json";
        private static readonly List<(Regex Pattern, string Replacement)> Patterns = LoadPatterns();
        private static readonly HashSet<string> SensitiveOptions = new(
            LoadPolicy().SensitiveOptions,
            StringComparer.OrdinalIgnoreCase);

        internal static string Redact(string? value)
        {
            string text = value ?? "";
            foreach ((Regex pattern, string replacement) in Patterns)
                text = pattern.Replace(text, replacement);
            return text;
        }

        internal static string RedactArguments(IReadOnlyList<string>? arguments)
        {
            var output = new List<string>();
            for (int index = 0; index < (arguments?.Count ?? 0); index++)
            {
                string value = arguments![index] ?? "";
                int equals = value.IndexOf('=');
                string option = equals > 0 ? value[..equals] : value;
                if (SensitiveOptions.Contains(option))
                {
                    output.Add(equals > 0 ? option + "=[REDACTED]" : option);
                    if (equals < 0 && index + 1 < arguments.Count)
                    {
                        output.Add("[REDACTED]");
                        index++;
                    }
                }
                else
                {
                    output.Add(Redact(value));
                }
            }
            return string.Join(" ", output);
        }

        private static List<(Regex Pattern, string Replacement)> LoadPatterns()
        {
            SensitivePolicyDocument document = LoadPolicy();
            var patterns = new List<(Regex Pattern, string Replacement)>();
            foreach (SensitivePatternDocument item in document.TextPatterns)
            {
                if (string.IsNullOrWhiteSpace(item.Pattern))
                    throw new InvalidOperationException("Sensitive policy contains an empty pattern");
                patterns.Add((new Regex(item.Pattern, RegexOptions.CultureInvariant), item.Replacement));
            }
            return patterns;
        }

        private static SensitivePolicyDocument LoadPolicy()
        {
            using Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(PolicyResource)
                ?? throw new InvalidOperationException($"Embedded sensitive policy not found: {PolicyResource}");
            SensitivePolicyDocument document = JsonSerializer.Deserialize<SensitivePolicyDocument>(stream)
                ?? throw new InvalidOperationException("Sensitive policy is empty");
            if (!string.Equals(document.Schema, "sensitive_policy.v1", StringComparison.Ordinal))
                throw new InvalidOperationException($"Unsupported sensitive policy schema: {document.Schema}");
            return document;
        }

        private sealed record SensitivePolicyDocument(
            [property: JsonPropertyName("schema")] string Schema,
            [property: JsonPropertyName("text_patterns")] IReadOnlyList<SensitivePatternDocument> TextPatterns,
            [property: JsonPropertyName("sensitive_options")] IReadOnlyList<string> SensitiveOptions);

        private sealed record SensitivePatternDocument(
            [property: JsonPropertyName("pattern")] string Pattern,
            [property: JsonPropertyName("replacement")] string Replacement);
    }
}
