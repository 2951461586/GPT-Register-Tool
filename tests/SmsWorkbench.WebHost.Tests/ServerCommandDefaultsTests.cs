namespace SmsWorkbench.WebHost.Tests;

public sealed class ServerCommandDefaultsTests
{
    [Fact]
    public void LoadReadsPythonPathAndProxyFromConfig()
    {
        string temp = Path.Combine(Path.GetTempPath(), "grt_defaults_" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(temp);
        File.WriteAllText(Path.Combine(temp, "config.json"), """
            {
              "runtime": { "python_path": "/usr/local/bin/python3" },
              "proxy": { "registration": "socks5://127.0.0.1:1080", "pool": ["http://p1:8080", "http://p2:8080"] },
              "email_registration": { "cfworker_domain": "mail.example.com" }
            }
            """);
        var paths = new RepositoryPaths(temp);
        var defaults = new ServerCommandDefaults(paths);
        CommandDefaults loaded = defaults.Load();

        Assert.Equal("/usr/local/bin/python3", loaded.PythonExecutable);
        Assert.Equal("socks5://127.0.0.1:1080", loaded.ProxyPool[0]);
        Assert.Equal(3, loaded.ProxyPool.Count);
        Assert.Equal("mail.example.com", loaded.CfWorkerDomain);
        Assert.Equal("smailr.com", loaded.SmailrDomain);
    }

    [Fact]
    public void LoadFallsBackWhenConfigMissing()
    {
        string temp = Path.Combine(Path.GetTempPath(), "grt_nocfg_" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(temp);
        // Also need chatgpt_phone_reg.py so RepositoryPaths finds root
        File.WriteAllText(Path.Combine(temp, "chatgpt_phone_reg.py"), "# stub");
        var paths = new RepositoryPaths(temp);
        var defaults = new ServerCommandDefaults(paths);
        CommandDefaults loaded = defaults.Load();

        Assert.Equal("python", loaded.PythonExecutable);
        Assert.Empty(loaded.ProxyPool);
    }
}
