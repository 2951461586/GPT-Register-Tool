// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Windows.Media;

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Factory for creating standard dialog windows with consistent styling.
    /// Eliminates the repeated Window + Grid + RowDefinitions boilerplate
    /// that appears in every dialog across MainWindow partial files.
    /// </summary>
    public static class DialogFactory
    {
        /// <summary>
        /// Create a standard dialog window pinned to the owner with the
        /// app background brush and center-owner startup location.
        /// </summary>
        public static Window Create(
            Window owner,
            string title,
            double width,
            double height,
            double minWidth = 0,
            double minHeight = 0,
            ResizeMode resizeMode = ResizeMode.NoResize)
        {
            var win = new Window
            {
                Title = title,
                Owner = owner,
                Width = width,
                Height = height,
                ResizeMode = resizeMode,
                WindowStartupLocation = WindowStartupLocation.CenterOwner,
                Background = (Brush)owner.FindResource("AppBg"),
            };
            if (minWidth > 0) win.MinWidth = minWidth;
            if (minHeight > 0) win.MinHeight = minHeight;
            return win;
        }

        /// <summary>
        /// Create the standard two-row root grid: a star row for body
        /// and an auto row for action buttons.
        /// </summary>
        public static Grid CreateRootGrid(double margin = 18)
        {
            var root = new Grid { Margin = new Thickness(margin) };
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            return root;
        }

        /// <summary>
        /// Create the standard action button row (right-aligned horizontal stack).
        /// </summary>
        public static StackPanel CreateActionRow()
        {
            return new StackPanel
            {
                Orientation = Orientation.Horizontal,
                HorizontalAlignment = HorizontalAlignment.Right,
            };
        }

        /// <summary>
        /// Two-column form grid: fixed label column (default 110) and a star value
        /// column. Rows are appended with <see cref="AddFormRow"/>,
        /// <see cref="AddValueRow"/> or <see cref="AddSpanningRow"/>.
        /// </summary>
        public static Grid CreateFormGrid(double labelWidth = 110)
        {
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(labelWidth) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            return grid;
        }

        /// <summary>Form label in the fixed column (sub-text colour, 10px right gap).</summary>
        public static TextBlock CreateFormLabel(Window owner, string text)
        {
            return new TextBlock
            {
                Text = text,
                VerticalAlignment = VerticalAlignment.Center,
                Margin = new Thickness(0, 0, 10, 10),
                Foreground = (Brush)owner.FindResource("TextSub"),
            };
        }

        /// <summary>Append a label/control row and return the row index.</summary>
        public static int AddFormRow(Grid grid, UIElement label, UIElement control)
        {
            int row = AddRow(grid);
            Grid.SetRow(label, row);
            Grid.SetColumn(label, 0);
            Grid.SetRow(control, row);
            Grid.SetColumn(control, 1);
            grid.Children.Add(label);
            grid.Children.Add(control);
            return row;
        }

        /// <summary>Append a row whose control sits in the value column; the label cell stays empty.</summary>
        public static int AddValueRow(Grid grid, UIElement control)
        {
            int row = AddRow(grid);
            Grid.SetRow(control, row);
            Grid.SetColumn(control, 1);
            grid.Children.Add(control);
            return row;
        }

        /// <summary>Append a row spanning every column (hints, section headers).</summary>
        public static int AddSpanningRow(Grid grid, UIElement element)
        {
            int row = AddRow(grid);
            Grid.SetRow(element, row);
            Grid.SetColumnSpan(element, Math.Max(1, grid.ColumnDefinitions.Count));
            grid.Children.Add(element);
            return row;
        }

        /// <summary>Standard dialog checkbox (main text colour, 10px bottom gap).</summary>
        public static CheckBox CreateCheckBox(Window owner, string text, bool isChecked = true)
        {
            return new CheckBox
            {
                Content = text,
                IsChecked = isChecked,
                Margin = new Thickness(0, 0, 0, 10),
                Foreground = (Brush)owner.FindResource("TextMain"),
            };
        }

        private static int AddRow(Grid grid)
        {
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            return grid.RowDefinitions.Count - 1;
        }

        /// <summary>
        /// Create a standard primary button with the PrimaryButton style.
        /// </summary>
        public static Button CreatePrimaryButton(Window owner, string text, double width = 88)
        {
            return new Button
            {
                Content = text,
                Width = width,
                Style = (Style)owner.FindResource("PrimaryButton"),
            };
        }

        /// <summary>
        /// Create a standard secondary (cancel) button.
        /// </summary>
        public static Button CreateCancelButton(string? text = null, double width = 76)
        {
            return new Button
            {
                Content = text ?? Loc.T("取消"),
                Width = width,
            };
        }

        // ── Async Helpers (P3: modernize dialog API) ──

        /// <summary>
        /// Show a simple info dialog with a single OK button. Uses async/await
        /// instead of blocking ShowDialog().
        /// </summary>
        public static async Task ShowInfoAsync(Window owner, string title, string message, string? okText = null)
        {
            var dialog = Create(owner, title, 420, 190, 380, 170);
            var root = CreateRootGrid();

            var body = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            body.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 8)
            });
            body.Children.Add(new TextBlock
            {
                Text = message,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)owner.FindResource("TextSub")
            });
            root.Children.Add(body);

            var okButton = CreatePrimaryButton(owner, okText ?? Loc.T("知道了"));
            okButton.HorizontalAlignment = HorizontalAlignment.Right;
            okButton.Click += (_, __) => dialog.Close();
            Grid.SetRow(okButton, 1);
            root.Children.Add(okButton);

            dialog.Content = root;
            await DialogShowAsync(dialog);
        }

        /// <summary>
        /// Show a confirmation dialog with Cancel + Confirm buttons.
        /// Returns true if the user clicked the confirm button.
        /// </summary>
        public static async Task<bool> ShowConfirmAsync(
            Window owner,
            string title,
            string message,
            string? confirmText = null,
            bool isDanger = false)
        {
            bool confirmed = false;
            var dialog = Create(owner, title, 460, 230, 420, 210);
            var root = CreateRootGrid();

            var body = new StackPanel { VerticalAlignment = VerticalAlignment.Center };
            body.Children.Add(new TextBlock
            {
                Text = title,
                FontSize = 18,
                FontWeight = FontWeights.SemiBold,
                Foreground = (Brush)owner.FindResource("TextMain"),
                Margin = new Thickness(0, 0, 0, 8)
            });
            body.Children.Add(new TextBlock
            {
                Text = message,
                TextWrapping = TextWrapping.Wrap,
                Foreground = (Brush)owner.FindResource("TextSub")
            });
            root.Children.Add(body);

            var actions = CreateActionRow();
            var cancelButton = CreateCancelButton();
            cancelButton.Click += (_, __) => dialog.Close();
            var confirmButton = new Button
            {
                Content = confirmText ?? Loc.T("确认"),
                Width = 76,
                Style = isDanger ? (Style)owner.FindResource("DangerButton") : (Style)owner.FindResource("PrimaryButton")
            };
            confirmButton.Click += (_, __) =>
            {
                confirmed = true;
                dialog.Close();
            };
            actions.Children.Add(cancelButton);
            actions.Children.Add(confirmButton);
            Grid.SetRow(actions, 1);
            root.Children.Add(actions);

            dialog.Content = root;
            await DialogShowAsync(dialog);
            return confirmed;
        }

        /// <summary>
        /// Show a dialog asynchronously using TaskCompletionSource,
        /// eliminating blocking ShowDialog() calls.
        /// </summary>
        private static Task DialogShowAsync(Window dialog)
        {
            var tcs = new TaskCompletionSource<bool>();
            dialog.Closed += (_, __) => tcs.TrySetResult(true);
            dialog.Show();
            return tcs.Task;
        }
    }
}
