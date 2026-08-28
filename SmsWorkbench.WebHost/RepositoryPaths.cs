namespace SmsWorkbench.WebHost;

public sealed class RepositoryPaths
{
    public RepositoryPaths(string baseDirectory)
    {
        RootDirectory = FindRoot(baseDirectory);
        BackendScriptPath = Path.Combine(RootDirectory, "chatgpt_phone_reg.py");
        ConfigPath = Path.Combine(RootDirectory, "config.json");
    }

    public string RootDirectory { get; }
    public string BackendScriptPath { get; }
    public string ConfigPath { get; }

    private static string FindRoot(string baseDirectory)
    {
        DirectoryInfo? current = new(Path.GetFullPath(baseDirectory));
        for (int depth = 0; current is not null && depth < 10; depth++, current = current.Parent)
        {
            if (File.Exists(Path.Combine(current.FullName, "chatgpt_phone_reg.py")))
                return current.FullName;
        }
        return Path.GetFullPath(baseDirectory);
    }
}
