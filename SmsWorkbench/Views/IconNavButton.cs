// Opted into nullable reference checking file-by-file - see the note in
// PaymentBatchService.cs for why the project-wide switch stays `annotations`.
#nullable enable

using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace SmsWorkbench
{
    /// <summary>
    /// 侧边栏导航按钮：把原先 16 处重复的
    /// <c>Grid/Border/Path/TextBlock</c> 内联结构抽成单一控件，
    /// 只暴露 <see cref="IconGeometry"/> / <see cref="Text"/> / <see cref="IsActive"/>
    /// 三个依赖属性（<c>IsActive</c> 表示该行动作正在运行，由 <c>Sidebar.ActiveKey</c> 驱动），
    /// 并将交互从事件处理器 (<c>Click=</c>) 改为 <c>Command=</c>。
    /// 外观由 <c>MainWindow.xaml</c> 里的 <c>IconNavButtonStyle</c> 定义。
    /// </summary>
    public class IconNavButton : Button
    {
        public static readonly DependencyProperty IconGeometryProperty =
            DependencyProperty.Register(
                nameof(IconGeometry),
                typeof(Geometry),
                typeof(IconNavButton),
                new PropertyMetadata(null));

        public static readonly DependencyProperty TextProperty =
            DependencyProperty.Register(
                nameof(Text),
                typeof(string),
                typeof(IconNavButton),
                new PropertyMetadata(null));

        /// <summary>
        /// True while this row's action is the one running. Read-only from the
        /// view's point of view: <c>Sidebar.xaml</c> feeds it through a
        /// MultiBinding of the button's own CommandParameter against
        /// <see cref="Sidebar.ActiveKey"/>, so no call site sets it by hand.
        /// </summary>
        public static readonly DependencyProperty IsActiveProperty =
            DependencyProperty.Register(
                nameof(IsActive),
                typeof(bool),
                typeof(IconNavButton),
                new PropertyMetadata(false));

        public Geometry IconGeometry
        {
            get => (Geometry)GetValue(IconGeometryProperty);
            set => SetValue(IconGeometryProperty, value);
        }

        public string Text
        {
            get => (string)GetValue(TextProperty);
            set => SetValue(TextProperty, value);
        }

        public bool IsActive
        {
            get => (bool)GetValue(IsActiveProperty);
            set => SetValue(IsActiveProperty, value);
        }
    }
}
