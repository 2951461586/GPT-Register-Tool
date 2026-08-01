namespace SmsWorkbench
{
    public static class PasswordBoxBinding
    {
        public static readonly DependencyProperty BoundPasswordProperty = DependencyProperty.RegisterAttached(
            "BoundPassword",
            typeof(string),
            typeof(PasswordBoxBinding),
            new FrameworkPropertyMetadata(null, FrameworkPropertyMetadataOptions.BindsTwoWayByDefault, OnBoundPasswordChanged));

        private static readonly DependencyProperty UpdatingProperty = DependencyProperty.RegisterAttached(
            "Updating",
            typeof(bool),
            typeof(PasswordBoxBinding));

        public static string GetBoundPassword(DependencyObject target) => target.GetValue(BoundPasswordProperty) as string ?? "";

        public static void SetBoundPassword(DependencyObject target, string value) => target.SetValue(BoundPasswordProperty, value);

        private static void OnBoundPasswordChanged(DependencyObject target, DependencyPropertyChangedEventArgs args)
        {
            if (target is not PasswordBox box || (bool)box.GetValue(UpdatingProperty)) return;
            box.PasswordChanged -= OnPasswordChanged;
            box.Password = args.NewValue as string ?? "";
            box.PasswordChanged += OnPasswordChanged;
        }

        private static void OnPasswordChanged(object sender, RoutedEventArgs args)
        {
            var box = (PasswordBox)sender;
            box.SetValue(UpdatingProperty, true);
            SetBoundPassword(box, box.Password);
            box.SetValue(UpdatingProperty, false);
        }
    }
}
