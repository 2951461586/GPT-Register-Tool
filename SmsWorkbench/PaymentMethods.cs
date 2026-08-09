using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace SmsWorkbench
{
    public sealed record PaymentMethodOption(string Id, string DisplayName);

    public sealed record PaymentMethodDefinition(
        string Id,
        string DisplayName,
        string DefaultCountry,
        string Currency,
        string Adapter,
        string RegistrationDisplayName,
        IReadOnlyList<string> Aliases,
        bool BatchEnabled = true,
        bool RegistrationEnabled = true)
    {
        public string SingleAccountDescription => RegistrationDisplayName;
    }

    internal sealed class PaymentMethodCatalogDocument
    {
        [JsonPropertyName("schema")]
        public string Schema { get; init; } = "";

        [JsonPropertyName("default_method")]
        public string DefaultMethod { get; init; } = "";

        [JsonPropertyName("methods")]
        public List<PaymentMethodDocument> Methods { get; init; } = [];
    }

    internal sealed class PaymentMethodDocument
    {
        [JsonPropertyName("id")]
        public string Id { get; init; } = "";

        [JsonPropertyName("display_name")]
        public string DisplayName { get; init; } = "";

        [JsonPropertyName("registration_display_name")]
        public string RegistrationDisplayName { get; init; } = "";

        [JsonPropertyName("country")]
        public string Country { get; init; } = "";

        [JsonPropertyName("currency")]
        public string Currency { get; init; } = "";

        [JsonPropertyName("adapter")]
        public string Adapter { get; init; } = "";

        [JsonPropertyName("aliases")]
        public List<string> Aliases { get; init; } = [];

        [JsonPropertyName("batch_enabled")]
        public bool BatchEnabled { get; init; } = true;

        [JsonPropertyName("registration_enabled")]
        public bool RegistrationEnabled { get; init; } = true;
    }

    public static class PaymentMethods
    {
        private const string CatalogSchema = "payment_methods.v1";
        private const string CatalogResource = "SmsWorkbench.payment_methods.json";
        private static readonly PaymentMethodCatalogDocument Catalog = LoadCatalog();
        private static readonly Dictionary<string, string> AliasMap = BuildAliasMap();

        public static IReadOnlyList<PaymentMethodDefinition> All { get; } = Catalog.Methods
            .Select(method => new PaymentMethodDefinition(
                method.Id,
                method.DisplayName,
                method.Country,
                method.Currency,
                method.Adapter,
                method.RegistrationDisplayName,
                method.Aliases,
                method.BatchEnabled,
                method.RegistrationEnabled))
            .ToArray();

        public static IReadOnlyList<PaymentMethodOption> BatchOptions { get; } = All
            .Where(method => method.BatchEnabled)
            .Select(method => new PaymentMethodOption(method.Id, method.RegistrationDisplayName))
            .ToArray();

        public static IReadOnlyList<PaymentMethodOption> RegistrationOptions { get; } = All
            .Where(method => method.BatchEnabled && method.RegistrationEnabled)
            .Select(method => new PaymentMethodOption(method.Id, method.RegistrationDisplayName))
            .ToArray();

        public static string Normalize(string? paymentMethod)
        {
            string value = NormalizeKey(paymentMethod);
            if (value.Length == 0)
                return Catalog.DefaultMethod;
            return AliasMap.TryGetValue(value, out string? normalized) ? normalized : "";
        }

        public static string DisplayName(string? paymentMethod)
            => Find(paymentMethod).DisplayName;

        public static PaymentMethodDefinition Find(string? paymentMethod)
        {
            string normalized = Normalize(paymentMethod);
            return All.FirstOrDefault(method => method.Id == normalized)
                ?? throw new ArgumentException($"Unsupported payment method: {paymentMethod}", nameof(paymentMethod));
        }

        private static PaymentMethodCatalogDocument LoadCatalog()
        {
            Assembly assembly = typeof(PaymentMethods).Assembly;
            using Stream stream = assembly.GetManifestResourceStream(CatalogResource)
                ?? throw new InvalidOperationException($"Embedded payment catalog not found: {CatalogResource}");
            PaymentMethodCatalogDocument catalog = JsonSerializer.Deserialize<PaymentMethodCatalogDocument>(stream)
                ?? throw new InvalidOperationException("Payment catalog is empty");
            if (!string.Equals(catalog.Schema, CatalogSchema, StringComparison.Ordinal))
                throw new InvalidOperationException($"Unsupported payment catalog schema: {catalog.Schema}");
            if (catalog.Methods.Count == 0)
                throw new InvalidOperationException("Payment catalog has no methods");
            if (!catalog.Methods.Any(method => method.Id == catalog.DefaultMethod))
                throw new InvalidOperationException($"Payment catalog default is invalid: {catalog.DefaultMethod}");
            if (catalog.Methods.Select(method => method.Id).Distinct(StringComparer.Ordinal).Count() != catalog.Methods.Count)
                throw new InvalidOperationException("Payment catalog contains duplicate method ids");
            return catalog;
        }

        private static Dictionary<string, string> BuildAliasMap()
        {
            var aliases = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (PaymentMethodDocument method in Catalog.Methods)
            {
                AddAlias(aliases, method.Id, method.Id);
                foreach (string alias in method.Aliases)
                    AddAlias(aliases, alias, method.Id);
            }
            return aliases;
        }

        private static void AddAlias(Dictionary<string, string> aliases, string value, string method)
        {
            string key = NormalizeKey(value);
            if (key.Length == 0)
                return;
            if (aliases.TryGetValue(key, out string? existing) && existing != method)
                throw new InvalidOperationException($"Duplicate payment catalog alias: {value}");
            aliases[key] = method;
        }

        private static string NormalizeKey(string? value)
            => (value ?? "").Trim().ToLowerInvariant().Replace(" ", "_");
    }
}
