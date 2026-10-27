// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using SmsWorkbench.Localization;

namespace SmsWorkbench
{
    /// <summary>
    /// Proxy-pool admin window: one grid, three read-only backends.
    /// <para>
    /// Built in code rather than XAML on purpose. The XAML variant needs the
    /// WPF-generated <c>InitializeComponent</c> partial, which the desktop's
    /// XAML generator only adds at build time; a window that is opened from a
    /// menu and disposed immediately (this one) gains nothing from a .xaml file,
    /// and several dialogs in this app are already code-built. The binding
    /// surface is identical either way.
    /// </para>
    /// </summary>
    public partial class ProxyPoolWindow : Window
    {
        public ProxyPoolWindow(ProxyPoolViewModel viewModel)
        {
            DataContext = viewModel;
            Title = Loc.T("代理池管理");
            Width = 1120;
            Height = 780;
            MinWidth = 900;
            MinHeight = 600;
            ResizeMode = ResizeMode.CanResize;
            WindowStartupLocation = WindowStartupLocation.CenterOwner;
            SetResourceReference(BackgroundProperty, "AppBg");
            Content = BuildLayout(viewModel);
        }

        private Grid BuildLayout(ProxyPoolViewModel viewModel)
        {
            var root = new Grid { Margin = new Thickness(20) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            root.Children.Add(BuildHeader());

            Grid controls = BuildControls(viewModel);
            Grid.SetRow(controls, 1);
            root.Children.Add(controls);

            Grid poolBox = BuildPoolBox(viewModel);
            Grid.SetRow(poolBox, 2);
            root.Children.Add(poolBox);

            DataGrid grid = BuildGrid();
            Grid.SetRow(grid, 3);
            root.Children.Add(grid);

            var status = new TextBlock
            {
                Margin = new Thickness(0, 10, 0, 0),
                TextWrapping = TextWrapping.Wrap,
            };
            status.SetBinding(TextBlock.TextProperty, new Binding(nameof(ProxyPoolViewModel.StatusText)));
            status.SetResourceReference(TextBlock.ForegroundProperty, "TextSub");
            Grid.SetRow(status, 4);
            root.Children.Add(status);

            return root;
        }

        private static Grid BuildHeader()
        {
            var header = new Grid { Margin = new Thickness(0, 0, 0, 12) };
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            header.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            var title = new TextBlock
            {
                Text = Loc.T("代理池管理"),
                FontSize = 20,
                FontWeight = FontWeights.SemiBold,
                VerticalAlignment = VerticalAlignment.Center,
            };
            header.Children.Add(title);

            var hint = new TextBlock
            {
                Text = Loc.T("只读诊断：不上号、不改配置"),
                VerticalAlignment = VerticalAlignment.Center,
            };
            hint.SetResourceReference(TextBlock.ForegroundProperty, "TextSub");
            Grid.SetColumn(hint, 1);
            header.Children.Add(hint);
            return header;
        }

        private static Grid BuildControls(ProxyPoolViewModel viewModel)
        {
            var controls = new Grid { Margin = new Thickness(0, 0, 0, 10) };
            for (int i = 0; i < 4; i++)
                controls.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            controls.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

            controls.Children.Add(Field(Loc.T("配置 lane"), viewModel, nameof(ProxyPoolViewModel.Lane), 0));
            controls.Children.Add(Field(Loc.T("目标国家"), viewModel, nameof(ProxyPoolViewModel.Country), 1));
            controls.Children.Add(Field(Loc.T("并发"), viewModel, nameof(ProxyPoolViewModel.Workers), 2));
            controls.Children.Add(Field(Loc.T("质量下限"), viewModel, nameof(ProxyPoolViewModel.QualityMin), 3));

            var buttons = new StackPanel
            {
                Orientation = Orientation.Horizontal,
                VerticalAlignment = VerticalAlignment.Bottom,
            };
            buttons.Children.Add(Button(Loc.T("加载配置池"), viewModel, nameof(ProxyPoolViewModel.LoadCommand)));
            buttons.Children.Add(Button(Loc.T("连通性测试"), viewModel, nameof(ProxyPoolViewModel.TestCommand)));
            buttons.Children.Add(Button(Loc.T("住宅筛选"), viewModel, nameof(ProxyPoolViewModel.ScreenCommand)));
            Grid.SetColumn(buttons, 4);
            controls.Children.Add(buttons);
            return controls;
        }

        private static StackPanel Field(string label, ProxyPoolViewModel viewModel, string property, int column)
        {
            var panel = new StackPanel { Margin = new Thickness(0, 0, 8, 0) };
            var caption = new TextBlock { Text = label, Margin = new Thickness(0, 0, 0, 4) };
            caption.SetResourceReference(TextBlock.ForegroundProperty, "TextSub");
            panel.Children.Add(caption);
            var box = new TextBox();
            box.SetBinding(TextBox.TextProperty, new Binding(property) { UpdateSourceTrigger = UpdateSourceTrigger.PropertyChanged });
            panel.Children.Add(box);
            Grid.SetColumn(panel, column);
            return panel;
        }

        private static Button Button(string label, ProxyPoolViewModel viewModel, string command)
        {
            var button = new Button
            {
                Content = label,
                Height = 30,
                Padding = new Thickness(12, 0, 12, 0),
                Margin = new Thickness(0, 0, 8, 0),
            };
            button.SetBinding(System.Windows.Controls.Primitives.ButtonBase.CommandProperty, new Binding(command));
            return button;
        }

        private static Grid BuildPoolBox(ProxyPoolViewModel viewModel)
        {
            var holder = new Grid { Margin = new Thickness(0, 0, 0, 10) };
            holder.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            holder.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });

            var caption = new TextBlock { Text = Loc.T("临时代理池（可选；每行一个，留空则用配置池）") };
            caption.SetResourceReference(TextBlock.ForegroundProperty, "TextSub");
            holder.Children.Add(caption);

            var box = new TextBox
            {
                Height = 64,
                AcceptsReturn = true,
                TextWrapping = TextWrapping.NoWrap,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                FontFamily = new FontFamily("Consolas"),
            };
            box.SetBinding(
                TextBox.TextProperty,
                new Binding(nameof(ProxyPoolViewModel.PoolText)) { UpdateSourceTrigger = UpdateSourceTrigger.PropertyChanged });
            Grid.SetRow(box, 1);
            holder.Children.Add(box);
            return holder;
        }

        private static DataGrid BuildGrid()
        {
            var grid = new DataGrid
            {
                AutoGenerateColumns = false,
                IsReadOnly = true,
                CanUserAddRows = false,
                HeadersVisibility = DataGridHeadersVisibility.Column,
                GridLinesVisibility = DataGridGridLinesVisibility.Horizontal,
                RowHeight = 28,
            };
            grid.SetBinding(ItemsControl.ItemsSourceProperty, new Binding(nameof(ProxyPoolViewModel.Rows)));
            grid.SetResourceReference(Control.BackgroundProperty, "PanelBg");
            grid.SetResourceReference(Control.BorderBrushProperty, "Line");
            grid.Columns.Add(StarColumn(Loc.T("代理"), nameof(ProxyPoolRow.Proxy)));
            grid.Columns.Add(Column(Loc.T("出口国家"), nameof(ProxyPoolRow.Country), 80));
            grid.Columns.Add(Column("ASN", nameof(ProxyPoolRow.Asn), 90));
            grid.Columns.Add(Column("ISP", nameof(ProxyPoolRow.Isp), 160));
            grid.Columns.Add(Column(Loc.T("网络类型"), nameof(ProxyPoolRow.NetworkType), 100));
            grid.Columns.Add(Column(Loc.T("边缘"), nameof(ProxyPoolRow.Edge), 90));
            grid.Columns.Add(Column(Loc.T("延迟(ms)"), nameof(ProxyPoolRow.Latency), 90));
            grid.Columns.Add(Column(Loc.T("质量分"), nameof(ProxyPoolRow.Quality), 80));
            grid.Columns.Add(Column(Loc.T("结论"), nameof(ProxyPoolRow.Result), 140));
            return grid;
        }

        private static DataGridTextColumn Column(string header, string path, double pixels)
            => new()
            {
                Header = header,
                Binding = new Binding(path),
                Width = new DataGridLength(pixels),
            };

        private static DataGridTextColumn StarColumn(string header, string path)
            => new()
            {
                Header = header,
                Binding = new Binding(path),
                Width = new DataGridLength(1, DataGridLengthUnitType.Star),
            };
    }
}
