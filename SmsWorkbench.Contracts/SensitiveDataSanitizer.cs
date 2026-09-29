// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

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

        /// <summary>
        /// Log-strength redaction: credentials plus log-only masking (account
        /// emails). Mirrors the backend's <c>sanitize_log_text</c>, so both
        /// sides of the IPC boundary apply the same rules from the same policy
        /// file. Every caller here feeds operator-visible output, never storage.
        /// </summary>
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
            // Credential rules first: they redact secrets that must never be
            // stored or printed. Log-only rules (account emails) run after, so
            // they only ever see text that has already been de-credentialed.
            AddPatterns(patterns, document.TextPatterns, "text_patterns");
            AddPatterns(patterns, document.LogTextPatterns, "log_text_patterns");
            return patterns;
        }

        private static void AddPatterns(
            List<(Regex Pattern, string Replacement)> target,
            IReadOnlyList<SensitivePatternDocument>? entries,
            string section)
        {
            if (entries is null)
            {
                // Older policies predate log_text_patterns; absence is legal.
                return;
            }
            foreach (SensitivePatternDocument item in entries)
            {
                if (item is null || string.IsNullOrWhiteSpace(item.Pattern))
                    throw new InvalidOperationException($"Sensitive policy contains an empty pattern in {section}");
                target.Add((new Regex(item.Pattern, RegexOptions.CultureInvariant), item.Replacement ?? "[REDACTED]"));
            }
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
            [property: JsonPropertyName("sensitive_options")] IReadOnlyList<string> SensitiveOptions,
            [property: JsonPropertyName("log_text_patterns")] IReadOnlyList<SensitivePatternDocument>? LogTextPatterns = null);

        private sealed record SensitivePatternDocument(
            [property: JsonPropertyName("pattern")] string Pattern,
            [property: JsonPropertyName("replacement")] string Replacement);
    }
}
