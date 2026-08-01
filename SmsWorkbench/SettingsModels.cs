using CommunityToolkit.Mvvm.ComponentModel;
using System.Collections.ObjectModel;

namespace SmsWorkbench
{
    public enum SettingFieldKind
    {
        Text,
        Secret,
        Number,
        Boolean,
        Options,
        Multiline
    }

    public sealed record SettingDefinition(
        string Key,
        string Label,
        string JsonPath,
        SettingFieldKind Kind,
        string DefaultValue = "",
        IReadOnlyList<string> Options = null);

    public sealed partial class SettingFieldViewModel : ObservableObject
    {
        [ObservableProperty] private string value = "";

        public SettingFieldViewModel(SettingDefinition definition, string initialValue)
        {
            Definition = definition;
            value = initialValue ?? "";
        }

        public SettingDefinition Definition { get; }
        public string Key => Definition.Key;
        public string Label => Definition.Label;
        public SettingFieldKind Kind => Definition.Kind;
        public IReadOnlyList<string> Options => Definition.Options ?? Array.Empty<string>();

        public bool BooleanValue
        {
            get => ParseBoolean(Value);
            set => Value = value ? "true" : "false";
        }

        partial void OnValueChanged(string value) => OnPropertyChanged(nameof(BooleanValue));

        private static bool ParseBoolean(string value)
            => string.Equals(value, "true", StringComparison.OrdinalIgnoreCase)
                || value == "1"
                || string.Equals(value, "yes", StringComparison.OrdinalIgnoreCase)
                || string.Equals(value, "on", StringComparison.OrdinalIgnoreCase);
    }

    public sealed class SettingsSectionViewModel
    {
        public SettingsSectionViewModel(string title, IEnumerable<SettingFieldViewModel> fields)
        {
            Title = title;
            Fields = new ObservableCollection<SettingFieldViewModel>(fields);
        }

        public string Title { get; }
        public ObservableCollection<SettingFieldViewModel> Fields { get; }
    }

    public sealed class SettingsCategoryViewModel
    {
        public SettingsCategoryViewModel(string title, IEnumerable<SettingsSectionViewModel> sections)
        {
            Title = title;
            Sections = new ObservableCollection<SettingsSectionViewModel>(sections);
        }

        public string Title { get; }
        public ObservableCollection<SettingsSectionViewModel> Sections { get; }
    }

    public sealed record SettingsSaveResult(bool Ok, string Error = "");
}
