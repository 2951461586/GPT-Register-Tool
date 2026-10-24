// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Collections.Concurrent;
using System.Runtime.CompilerServices;
using System.Windows.Markup;
using System.Windows.Threading;

namespace SmsWorkbench.Localization
{
    /// <summary>
    /// Markup extension for XAML text: <c>{loc:Tr Key='一键注册'}</c>.
    ///
    /// It returns a one-way binding to <see cref="Loc.Instance"/> through
    /// <see cref="TranslationConverter"/> rather than a raw string, so switching
    /// the language updates every already-realized control without a restart.
    /// The Chinese source string is the key (see <see cref="StringsEn"/>).
    /// </summary>
    [MarkupExtensionReturnType(typeof(object))]
    public sealed class TrExtension : MarkupExtension
    {
        public TrExtension()
        {
        }

        public TrExtension(string key)
        {
            Key = key;
        }

        [ConstructorArgument("key")]
        public string Key { get; set; } = "";

        public override object ProvideValue(IServiceProvider serviceProvider)
        {
            if (serviceProvider?.GetService(typeof(IProvideValueTarget)) is IProvideValueTarget target
                && target.TargetObject is DependencyObject targetObject
                && target.TargetProperty is DependencyProperty targetProperty)
            {
                TranslationBindingRegistry.Register(targetObject, targetProperty);
            }

            var binding = new Binding(".")
            {
                Source = Loc.Instance,
                Mode = BindingMode.OneWay,
                Converter = TranslationConverter.Instance,
                ConverterParameter = Key,
            };
            return binding.ProvideValue(serviceProvider);
        }
    }

    /// <summary>Maps a binding value through <see cref="Loc.Translate"/> using the key in the parameter.</summary>
    public sealed class TranslationConverter : IValueConverter
    {
        public static readonly TranslationConverter Instance = new();

        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
            => parameter is string key ? Loc.Instance.Translate(key) : value;

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
            => throw new NotSupportedException();
    }

    /// <summary>
    /// Formats the staged-proxy pool summary. WPF binds <c>StringFormat</c> too
    /// early to be localized, so the prefix goes through this converter instead.
    /// </summary>
    public sealed class PoolSummaryConverter : IValueConverter
    {
        public object Convert(object value, Type targetType, object parameter, CultureInfo culture)
            => Loc.F("池构成：{0}", value);

        public object ConvertBack(object value, Type targetType, object parameter, CultureInfo culture)
            => throw new NotSupportedException();
    }

    /// <summary>
    /// Keeps the (target, property) pairs created by <see cref="TrExtension"/>.
    ///
    /// WPF does not re-evaluate a binding whose path is "." when the source
    /// raises <c>PropertyChanged("")</c> -- measured 2026-10-24: the control kept
    /// the old language.  Updating the recorded binding expressions explicitly is
    /// the part that actually makes a live language switch work.
    /// </summary>
    public static class TranslationBindingRegistry
    {
        private static readonly ConditionalWeakTable<DependencyObject, List<DependencyProperty>> Registered = new();

        public static void Register(DependencyObject target, DependencyProperty property)
        {
            lock (Registered)
            {
                List<DependencyProperty> properties = Registered.GetOrCreateValue(target);
                if (!properties.Contains(property))
                {
                    properties.Add(property);
                }
            }
        }

        public static void RefreshAll()
        {
            var subscriptions = new List<(DependencyObject Target, DependencyProperty Property)>();
            lock (Registered)
            {
                foreach (KeyValuePair<DependencyObject, List<DependencyProperty>> entry in Registered)
                {
                    foreach (DependencyProperty property in entry.Value)
                    {
                        subscriptions.Add((entry.Key, property));
                    }
                }
            }

            foreach ((DependencyObject target, DependencyProperty property) in subscriptions)
            {
                try
                {
                    if (target is DispatcherObject dispatcherObject && !dispatcherObject.CheckAccess())
                    {
                        // Tests and any future background switch run off the UI
                        // thread; WPF binding work must happen on the owner's one.
                        // BeginInvoke, not Invoke: a synchronous marshal blocks the
                        // language switch whenever the owner's dispatcher is not
                        // pumping (a closed window, or a previous test's STA thread).
                        // Measured 2026-10-24: with serialized xUnit collections the
                        // smoke-test windows left registered targets behind and the
                        // next switch deadlocked for 15s. Cross-thread updates are
                        // fire-and-forget by nature; same-thread switches still take
                        // the synchronous branch above.
                        dispatcherObject.Dispatcher.BeginInvoke(
                            () => BindingOperations.GetBindingExpression(target, property)?.UpdateTarget());
                    }
                    else
                    {
                        BindingOperations.GetBindingExpression(target, property)?.UpdateTarget();
                    }
                }
                catch (Exception)
                {
                    // A torn-down window/dispatcher must not break the switch.
                }
            }
        }

        public static void Unregister(DependencyObject target, DependencyProperty property)
        {
            lock (Registered)
            {
                if (Registered.TryGetValue(target, out List<DependencyProperty>? properties))
                {
                    properties.Remove(property);
                }
            }
        }
    }
}
