// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

namespace SmsWorkbench
{
    public partial class MainWindow
    {
        // Commands bound from MainWindow.xaml (row context menu and the row detail
        // button). Created on first use because property initializers cannot capture
        // the instance methods they call.

        private RelayCommand? ctxViewDetailCommand;
        public RelayCommand CtxViewDetailCommand => ctxViewDetailCommand ??= new RelayCommand(CtxViewDetail);

        private RelayCommand? ctxViewInboxCommand;
        public RelayCommand CtxViewInboxCommand => ctxViewInboxCommand ??= new RelayCommand(CtxViewInbox);

        private RelayCommand? ctxCopyEmailCommand;
        public RelayCommand CtxCopyEmailCommand => ctxCopyEmailCommand ??= new RelayCommand(CtxCopyEmail);

        private RelayCommand? ctxCopyAccessTokenCommand;
        public RelayCommand CtxCopyAccessTokenCommand => ctxCopyAccessTokenCommand ??= new RelayCommand(CtxCopyAccessToken);

        private RelayCommand? ctxCopyPayPalCommand;
        public RelayCommand CtxCopyPayPalCommand => ctxCopyPayPalCommand ??= new RelayCommand(CtxCopyPayPal);

        private RelayCommand? ctxOpenPayPalCommand;
        public RelayCommand CtxOpenPayPalCommand => ctxOpenPayPalCommand ??= new RelayCommand(CtxOpenPayPal);

        private RelayCommand? ctxOpenSourceCommand;
        public RelayCommand CtxOpenSourceCommand => ctxOpenSourceCommand ??= new RelayCommand(CtxOpenSource);

        private RelayCommand? ctxCheckAccountAliveCommand;
        public RelayCommand CtxCheckAccountAliveCommand => ctxCheckAccountAliveCommand ??= new RelayCommand(CtxCheckAccountAlive);

        private RelayCommand? ctxBatchProtocolPaymentCommand;
        public RelayCommand CtxBatchProtocolPaymentCommand => ctxBatchProtocolPaymentCommand ??= new RelayCommand(BatchProtocolPayment);

        private RelayCommand? ctxChangeEmailCommand;
        public RelayCommand CtxChangeEmailCommand => ctxChangeEmailCommand ??= new RelayCommand(CtxChangeEmail);

        private RelayCommand<PoolRow>? accountDetailCommand;
        public RelayCommand<PoolRow> AccountDetailCommand => accountDetailCommand ??= new RelayCommand<PoolRow>(row =>
        {
            if (row != null) ShowAccountDetail(row);
        });
    }
}
