namespace SmsWorkbench
{
    /// <summary>
    /// Generic JSON-to-dictionary plumbing shared by the window-independent
    /// backend interpreters and the MainWindow partials. This module owns the
    /// canonical <see cref="Dictionary{TKey, TValue}"/> projection of backend
    /// JSON so command planners and result interpreters never depend on WPF.
    /// </summary>
    public static class BackendJson
    {
        public static Dictionary<string, object> TextToObject(string json)
        {
            using JsonDocument document = JsonDocument.Parse(json);
            return DocumentToObject(document);
        }

        public static Dictionary<string, object> DocumentToObject(JsonDocument document)
        {
            var output = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            if (document.RootElement.ValueKind != JsonValueKind.Object) return output;
            foreach (JsonProperty property in document.RootElement.EnumerateObject())
            {
                output[property.Name] = ValueToObject(property.Value);
            }
            return output;
        }

        public static Dictionary<string, object> ElementToDictionary(JsonElement element)
        {
            return TextToObject(element.GetRawText());
        }

        public static object ValueToObject(JsonElement element)
        {
            switch (element.ValueKind)
            {
                case JsonValueKind.String: return element.GetString() ?? "";
                case JsonValueKind.Number:
                    return element.TryGetInt64(out long n) ? n : element.GetDouble();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                case JsonValueKind.Object:
                    var obj = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
                    foreach (JsonProperty property in element.EnumerateObject()) obj[property.Name] = ValueToObject(property.Value);
                    return obj;
                case JsonValueKind.Array:
                    return element.EnumerateArray().Select(ValueToObject).ToList();
                default: return "";
            }
        }

        public static string GetString(Dictionary<string, object> data, string key)
        {
            return data != null && data.TryGetValue(key, out object value) && value != null
                ? Convert.ToString(value, CultureInfo.InvariantCulture) ?? ""
                : "";
        }

        public static bool TryGetMap(Dictionary<string, object> data, string key, out Dictionary<string, object> map)
        {
            map = null;
            if (data == null || !data.TryGetValue(key, out object value)) return false;
            map = value as Dictionary<string, object>;
            return map != null;
        }

        public static string NestedString(Dictionary<string, object> data, params string[] path)
        {
            object current = data;
            foreach (string key in path)
            {
                if (current is not Dictionary<string, object> map) return "";
                if (!map.TryGetValue(key, out current)) return "";
            }
            return Convert.ToString(current, CultureInfo.InvariantCulture) ?? "";
        }

        public static bool GetBool(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.TryGetValue(key, out object value) || value == null) return false;
            if (value is bool b) return b;
            string text = Convert.ToString(value, CultureInfo.InvariantCulture)?.Trim() ?? "";
            return text.Equals("true", StringComparison.OrdinalIgnoreCase) || text == "1";
        }

        public static long GetLong(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.TryGetValue(key, out object val) || val == null) return 0;
            if (val is long l) return l;
            if (val is int i) return i;
            if (val is double d) return (long)d;
            if (long.TryParse(val.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out long parsed)) return parsed;
            return 0;
        }

        public static double GetDouble(Dictionary<string, object> data, string key)
        {
            if (data == null || !data.TryGetValue(key, out object val) || val == null) return 0;
            if (val is double d) return d;
            if (val is long l) return l;
            if (val is int i) return i;
            if (double.TryParse(val.ToString(), NumberStyles.Float, CultureInfo.InvariantCulture, out double parsed)) return parsed;
            return 0;
        }

        public static string FirstNonEmpty(params string[] values)
        {
            foreach (string value in values)
            {
                if (!string.IsNullOrWhiteSpace(value)) return value.Trim();
            }
            return "";
        }

        public static bool ParseBoolean(string value)
        {
            return string.Equals(value, "true", StringComparison.OrdinalIgnoreCase)
                || string.Equals(value, "yes", StringComparison.OrdinalIgnoreCase)
                || string.Equals(value, "on", StringComparison.OrdinalIgnoreCase)
                || value == "1";
        }

        public static string JwtAuthString(string token, string key)
        {
            try
            {
                string[] parts = (token ?? "").Split('.');
                if (parts.Length < 2 || parts[1].Length == 0) return "";
                string payload = parts[1].Replace('-', '+').Replace('_', '/');
                payload = payload.PadRight(payload.Length + ((4 - payload.Length % 4) % 4), '=');
                string json = Encoding.UTF8.GetString(Convert.FromBase64String(payload));
                var obj = TextToObject(json);
                if (TryGetMap(obj, "https://api.openai.com/auth", out Dictionary<string, object> auth))
                {
                    return GetString(auth, key);
                }
                return GetString(obj, key);
            }
            catch
            {
                return "";
            }
        }
    }
}
