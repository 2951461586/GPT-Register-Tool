namespace SmsWorkbench.Dialogs
{
    /// <summary>
    /// The three option dialogs the register/scan actions open.
    /// </summary>
    public interface IRegisterDialogService
    {
        /// <summary>One-click registration options; null when the operator cancels.</summary>
        RegisterOptions? ShowRegisterOptions(Window owner, int initialCount, int defaultWorkers);

        /// <summary>Options for registering the selected rows; null on cancel.</summary>
        RegisterOptions? ShowSelectedRegisterOptions(
            Window owner,
            int selectedCount,
            int checkedCount,
            int defaultWorkers);

        /// <summary>Account-scan options; null on cancel.</summary>
        ScanOptions? ShowScanOptions(Window owner, int accountCount);
    }

    public sealed class RegisterDialogService : IRegisterDialogService
    {
        public RegisterOptions? ShowRegisterOptions(Window owner, int initialCount, int defaultWorkers)
        {
            RegisterOptions? selected = null;
            Window dialog = CreateRegisterOptionsDialog(
                owner,
                initialCount,
                defaultWorkers,
                options => selected = options,
                onCountChosen: null);
            return dialog.ShowDialog() == true ? selected : null;
        }

        internal Window CreateRegisterOptionsDialog(
            Window owner,
            int initialCount,
            int defaultWorkers,
            Action<RegisterOptions> accept,
            Action<int>? onCountChosen)
        {
            return new RegisterOptionsDialog(owner, initialCount, defaultWorkers, accept, onCountChosen);
        }

        public RegisterOptions? ShowSelectedRegisterOptions(
            Window owner,
            int selectedCount,
            int checkedCount,
            int defaultWorkers)
        {
            RegisterOptions? selected = null;
            Window dialog = CreateSelectedRegisterOptionsDialog(
                owner,
                selectedCount,
                defaultWorkers,
                checkedCount,
                options => selected = options);
            return dialog.ShowDialog() == true ? selected : null;
        }

        internal Window CreateSelectedRegisterOptionsDialog(
            Window owner,
            int selectedCount,
            int defaultWorkers,
            int checkedCount,
            Action<RegisterOptions> accept)
        {
            return new SelectedRegisterOptionsDialog(owner, selectedCount, defaultWorkers, checkedCount, accept);
        }

        public ScanOptions? ShowScanOptions(Window owner, int accountCount)
        {
            var dialog = new ScanOptionsDialog(owner, accountCount);
            return dialog.ShowDialog() == true ? dialog.SelectedOptions : null;
        }
    }
}
