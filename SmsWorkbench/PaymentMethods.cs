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
        bool RegistrationEnabled = true,
        IReadOnlyList<string>? Stages = null)
    {
        public string SingleAccountDescription => RegistrationDisplayName;
    }

    internal sealed class PaymentCountryDocument
    {
        [JsonPropertyName("code")]
        public string Code { get; init; } = "";

        [JsonPropertyName("label")]
        public string Label { get; init; } = "";
    }

    internal sealed class PaymentMethodCatalogDocument
    {
        [JsonPropertyName("schema")]
        public string Schema { get; init; } = "";

        [JsonPropertyName("default_method")]
        public string DefaultMethod { get; init; } = "";

        [JsonPropertyName("checkout_countries")]
        public List<PaymentCountryDocument> CheckoutCountries { get; init; } = [];

        [JsonPropertyName("approve_countries")]
        public List<PaymentCountryDocument> ApproveCountries { get; init; } = [];

        [JsonPropertyName("stage_countries")]
        public List<PaymentCountryDocument> StageCountries { get; init; } = [];

        [JsonPropertyName("billing_countries")]
        public List<PaymentCountryDocument> BillingCountries { get; init; } = [];

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

        [JsonPropertyName("checkout_countries")]
        public List<PaymentCountryDocument>? CheckoutCountries { get; init; }

        [JsonPropertyName("approve_countries")]
        public List<PaymentCountryDocument>? ApproveCountries { get; init; }

        [JsonPropertyName("batch_enabled")]
        public bool BatchEnabled { get; init; } = true;

        [JsonPropertyName("registration_enabled")]
        public bool RegistrationEnabled { get; init; } = true;

        [JsonPropertyName("stages")]
        public List<string> Stages { get; init; } = [];
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
                method.RegistrationEnabled,
                method.Stages))
            .ToArray();

        public static IReadOnlyList<PaymentMethodOption> BatchOptions { get; } = All
            .Where(method => method.BatchEnabled)
            .Select(method => new PaymentMethodOption(method.Id, method.RegistrationDisplayName))
            .ToArray();

        public static IReadOnlyList<PaymentMethodOption> RegistrationOptions { get; } = All
            .Where(method => method.BatchEnabled && method.RegistrationEnabled)
            .Select(method => new PaymentMethodOption(method.Id, method.RegistrationDisplayName))
            .ToArray();

        public static IReadOnlyList<PaymentProxyCountryOption> StageCountryOptions { get; } = Catalog.StageCountries
            .Select(country => new PaymentProxyCountryOption(country.Code, country.Label))
            .ToArray();

        public static IReadOnlyList<PaymentProxyCountryOption> BillingCountryOptions { get; } = Catalog.BillingCountries
            .Select(country => new PaymentProxyCountryOption(country.Code, country.Label))
            .ToArray();

        public static IReadOnlyList<PaymentProxyCountryOption> CheckoutCountryOptions(string? paymentMethod)
            => ResolveCheckoutCountryOptions(Catalog, Normalize(paymentMethod));

        public static IReadOnlyList<PaymentProxyCountryOption> ApproveCountryOptions(string? paymentMethod)
            // Approve / Update must expose the same selectable region universe
            // as Checkout; routing still validates the chosen country per stage.
            => ResolveCheckoutCountryOptions(Catalog, Normalize(paymentMethod));

        public static string Normalize(string? paymentMethod)
        {
            string value = NormalizeKey(paymentMethod);
            if (value.Length == 0)
                return Catalog.DefaultMethod;
            return AliasMap.TryGetValue(value, out string? normalized) ? normalized : "";
        }

        public static string DisplayName(string? paymentMethod)
            => Find(paymentMethod).DisplayName;

        public static string DefaultUpdateCountry(string? paymentMethod, string? fallbackCountry = null)
        {
            string normalized = Normalize(paymentMethod);
            if (normalized == "gopay")
                return "TH";
            string fallback = (fallbackCountry ?? "").Trim().ToUpperInvariant();
            return fallback.Length > 0 ? fallback : Find(normalized).DefaultCountry;
        }

        public static PaymentMethodDefinition Find(string? paymentMethod)
        {
            string normalized = Normalize(paymentMethod);
            return All.FirstOrDefault(method => method.Id == normalized)
                ?? throw new ArgumentException($"Unsupported payment method: {paymentMethod}", nameof(paymentMethod));
        }

        internal static PaymentMethodCatalogDocument ParseCatalog(string json)
        {
            PaymentMethodCatalogDocument catalog = JsonSerializer.Deserialize<PaymentMethodCatalogDocument>(json)
                ?? throw new InvalidOperationException("Payment catalog is empty");
            ValidateCatalog(catalog);
            return catalog;
        }

        internal static IReadOnlyList<PaymentProxyCountryOption> ResolveCheckoutCountryOptions(
            PaymentMethodCatalogDocument catalog,
            string? paymentMethod)
            => ResolveCountryOptions(
                catalog,
                paymentMethod,
                method => method.CheckoutCountries,
                catalog.CheckoutCountries);

        internal static IReadOnlyList<PaymentProxyCountryOption> ResolveApproveCountryOptions(
            PaymentMethodCatalogDocument catalog,
            string? paymentMethod)
            => ResolveCountryOptions(
                catalog,
                paymentMethod,
                method => method.ApproveCountries,
                catalog.ApproveCountries);

        private static PaymentProxyCountryOption[] ResolveCountryOptions(
            PaymentMethodCatalogDocument catalog,
            string? paymentMethod,
            Func<PaymentMethodDocument, List<PaymentCountryDocument>?> methodOverride,
            IReadOnlyList<PaymentCountryDocument> topLevelDefault)
        {
            string normalized = NormalizeKey(paymentMethod);
            PaymentMethodDocument? method = normalized.Length == 0
                ? null
                : catalog.Methods.FirstOrDefault(candidate => candidate.Id == normalized);
            IReadOnlyList<PaymentCountryDocument> source = method != null
                && methodOverride(method) is { Count: > 0 } overrideCountries
                ? overrideCountries
                : topLevelDefault;
            return source
                .Select(country => new PaymentProxyCountryOption(country.Code, country.Label))
                .ToArray();
        }

        private static PaymentMethodCatalogDocument LoadCatalog()
        {
            Assembly assembly = typeof(PaymentMethods).Assembly;
            using Stream stream = assembly.GetManifestResourceStream(CatalogResource)
                ?? throw new InvalidOperationException($"Embedded payment catalog not found: {CatalogResource}");
            PaymentMethodCatalogDocument catalog = JsonSerializer.Deserialize<PaymentMethodCatalogDocument>(stream)
                ?? throw new InvalidOperationException("Payment catalog is empty");
            ValidateCatalog(catalog);
            return catalog;
        }

        private static void ValidateCatalog(PaymentMethodCatalogDocument catalog)
        {
            if (!string.Equals(catalog.Schema, CatalogSchema, StringComparison.Ordinal))
                throw new InvalidOperationException($"Unsupported payment catalog schema: {catalog.Schema}");
            if (catalog.Methods.Count == 0)
                throw new InvalidOperationException("Payment catalog has no methods");
            if (!catalog.Methods.Any(method => method.Id == catalog.DefaultMethod))
                throw new InvalidOperationException($"Payment catalog default is invalid: {catalog.DefaultMethod}");
            if (catalog.Methods.Select(method => method.Id).Distinct(StringComparer.Ordinal).Count() != catalog.Methods.Count)
                throw new InvalidOperationException("Payment catalog contains duplicate method ids");
            ValidateCountryOptions(catalog.CheckoutCountries, "checkout_countries", null);
            ValidateCountryOptions(catalog.ApproveCountries, "approve_countries", null);
            ValidateCountryOptions(catalog.StageCountries, "stage_countries", null);
            ValidateCountryOptions(catalog.BillingCountries, "billing_countries", null);
            foreach (PaymentMethodDocument method in catalog.Methods)
            {
                if (method.CheckoutCountries != null)
                    ValidateCountryOptions(method.CheckoutCountries, "checkout_countries", method.Id);
                if (method.ApproveCountries != null)
                    ValidateCountryOptions(method.ApproveCountries, "approve_countries", method.Id);
            }
        }

        private static void ValidateCountryOptions(
            List<PaymentCountryDocument> countries,
            string listName,
            string? methodId)
        {
            string owner = methodId == null
                ? $"top-level {listName}"
                : $"method '{methodId}' {listName}";
            if (countries.Count == 0)
                throw new InvalidOperationException($"Payment catalog {owner} must contain at least one country");
            foreach (PaymentCountryDocument country in countries)
            {
                if (!Regex.IsMatch(country.Code ?? "", "^[A-Z]{2}$", RegexOptions.CultureInvariant))
                    throw new InvalidOperationException($"Payment catalog {owner} has invalid country code: '{country.Code}'");
                if (string.IsNullOrWhiteSpace(country.Label))
                    throw new InvalidOperationException($"Payment catalog {owner} entry '{country.Code}' is missing a label");
            }
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
