namespace SmsWorkbench
{
    public partial class MainWindow
    {
        private void ShowConfigDialog()
        {
            if (settingsDialogs.ShowDialog(this))
                Log("配置已保存。");
        }

        private sealed class ConfigComboOption
        {
            public ConfigComboOption(string value, string label, string metadata = "", string extra = "")
            {
                Value = value;
                Label = label;
                Metadata = metadata;
                Extra = extra;
            }

            public string Value { get; }
            public string Label { get; }
            public string Metadata { get; }
            public string Extra { get; }
            public override string ToString() => Label;
        }

        private Dictionary<string, object> GetSection(Dictionary<string, object> config, string section)
        {
            if (config.TryGetValue(section, out object value) && value is Dictionary<string, object> map)
                return map;
            var created = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            config[section] = created;
            return created;
        }

        private Dictionary<string, object> GetChildSection(Dictionary<string, object> parent, string key)
        {
            if (parent.TryGetValue(key, out object value) && value is Dictionary<string, object> map)
                return map;
            var created = new Dictionary<string, object>(StringComparer.OrdinalIgnoreCase);
            parent[key] = created;
            return created;
        }

        private string FirstListValue(Dictionary<string, object> data, string key)
        {
            if (data.TryGetValue(key, out object value) && value is List<object> list && list.Count > 0)
                return Convert.ToString(list[0]) ?? "";
            return "";
        }

        private void SaveConfig(string path, Dictionary<string, object> config)
        {
            File.WriteAllText(
                path,
                JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true }),
                new UTF8Encoding(false));
        }

        private void EnsureConfigFile(string path)
        {
            if (File.Exists(path)) return;
            string example = Path.Combine(rootDir, "config.example.json");
            if (File.Exists(example)) File.Copy(example, path);
            else File.WriteAllText(path, "{}", new UTF8Encoding(false));
        }
    }
}
