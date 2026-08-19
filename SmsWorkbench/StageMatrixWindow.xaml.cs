namespace SmsWorkbench
{
    public partial class StageMatrixWindow : Window
    {
        public StageMatrixWindow(StageMatrixViewModel viewModel)
        {
            InitializeComponent();
            DataContext = viewModel ?? throw new ArgumentNullException(nameof(viewModel));
        }
    }
}
