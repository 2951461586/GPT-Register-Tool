using System.IO.Compression;
using System.Text;

namespace SmsWorkbench.Tests;

/// <summary>
/// The workspace archive: allow-list only, and a restore that cannot write outside the
/// roots it owns.
/// </summary>
public sealed class WorkspaceBackupServiceTests
{
    private static void SeedWorkspace(string root)
    {
        Directory.CreateDirectory(Path.Combine(root, "runtime"));
        File.WriteAllText(Path.Combine(root, "proxy.json"), "{\"pool\":[\"http://a:1\"]}");
        File.WriteAllText(Path.Combine(root, "runtime", "accounts.sqlite3"), "db-bytes");
        File.WriteAllText(Path.Combine(root, "runtime", "desktop_theme.txt"), "Dark");
        // Never part of a backup: regenerated or noisy.
        File.WriteAllText(Path.Combine(root, "runtime", "app_20261028.log"), "log noise");
        Directory.CreateDirectory(Path.Combine(root, "runtime", "cache"));
        File.WriteAllText(Path.Combine(root, "runtime", "cache", "blob.bin"), "cache");
    }

    [Fact]
    public void ExportKeepsOnlyTheAllowList()
    {
        using var fixture = new TemporaryDirectory();
        SeedWorkspace(fixture.Path);
        var service = new WorkspaceBackupService(new TestApplicationPaths(fixture.Path));
        string archivePath = Path.Combine(fixture.Path, "backup.zip");

        WorkspaceBackupResult result = service.Export(archivePath);

        using var archive = ZipFile.OpenRead(archivePath);
        string[] names = archive.Entries.Select(entry => entry.FullName).OrderBy(name => name, StringComparer.Ordinal).ToArray();
        Assert.Equal(
            new[]
            {
                "config/proxy.json",
                "runtime/accounts.sqlite3",
                "runtime/desktop_theme.txt",
                WorkspaceBackupService.ManifestEntry,
            },
            names);
        Assert.Equal(3, result.Entries);
        Assert.True(result.Bytes > 0);
        Assert.DoesNotContain(names, name => name.Contains("app_", StringComparison.Ordinal));
        Assert.DoesNotContain(names, name => name.Contains("cache", StringComparison.Ordinal));
    }

    [Fact]
    public void RestorePutsFilesBackAndKeepsThePreviousCopy()
    {
        using var fixture = new TemporaryDirectory();
        SeedWorkspace(fixture.Path);
        var service = new WorkspaceBackupService(new TestApplicationPaths(fixture.Path));
        string archivePath = Path.Combine(fixture.Path, "backup.zip");
        service.Export(archivePath);

        // The install moves on, then is rolled back.
        File.WriteAllText(Path.Combine(fixture.Path, "runtime", "desktop_theme.txt"), "Light");
        File.WriteAllText(Path.Combine(fixture.Path, "runtime", "accounts.sqlite3"), "newer-db");

        WorkspaceRestoreResult result = service.Restore(archivePath);

        Assert.Equal(3, result.Restored);
        Assert.Equal(0, result.Skipped);
        Assert.Equal("Dark", File.ReadAllText(Path.Combine(fixture.Path, "runtime", "desktop_theme.txt")));
        Assert.Equal("db-bytes", File.ReadAllText(Path.Combine(fixture.Path, "runtime", "accounts.sqlite3")));
        // A restore is itself undoable: the pre-restore copies sit next to the files.
        Assert.False(string.IsNullOrEmpty(result.SafetyDirectory));
        string[] safety = Directory.GetFiles(result.SafetyDirectory);
        Assert.Contains(safety, path => path.EndsWith("accounts.sqlite3", StringComparison.Ordinal));
        Assert.Contains("newer-db", File.ReadAllText(safety.Single(path => path.EndsWith("accounts.sqlite3", StringComparison.Ordinal))));
    }

    [Fact]
    public void ConfiguredMailboxPoolFileTravelsWithTheArchive()
    {
        using var fixture = new TemporaryDirectory();
        SeedWorkspace(fixture.Path);
        string poolPath = Path.Combine(fixture.Path, "runtime", "mailbox_pool.txt");
        File.WriteAllText(poolPath, "a@example.com----pw----client----refresh");
        // The pool path is configuration, not a constant: the archive has to read it from
        // the shard the backend reads.
        File.WriteAllText(
            Path.Combine(fixture.Path, "runtime.json"),
            "{\"email_registration\":{\"token_file\":\"" + poolPath.Replace("\\", "/") + "\"}}");

        var service = new WorkspaceBackupService(new TestApplicationPaths(fixture.Path));
        string archivePath = Path.Combine(fixture.Path, "backup.zip");
        service.Export(archivePath);

        using (var archive = ZipFile.OpenRead(archivePath))
        {
            Assert.Contains(
                archive.Entries,
                entry => entry.FullName == "runtime/mailbox_pool.txt");
        }

        // Lose it, then restore the archive.
        File.Delete(poolPath);
        WorkspaceRestoreResult result = service.Restore(archivePath);

        // proxy.json + runtime.json + the account DB + the theme file + the pool file.
        Assert.Equal(5, result.Restored);
        Assert.True(File.Exists(poolPath));
        Assert.Contains("a@example.com", File.ReadAllText(poolPath));
    }

    [Fact]
    public void RestoreRefusesEntriesOutsideTheAllowList()
    {
        using var fixture = new TemporaryDirectory();
        SeedWorkspace(fixture.Path);
        string archivePath = Path.Combine(fixture.Path, "hostile.zip");
        using (var stream = File.Create(archivePath))
        using (var archive = new ZipArchive(stream, ZipArchiveMode.Create))
        {
            WriteEntry(archive, "runtime/../escaped.txt", "pwned");
            WriteEntry(archive, "runtime/desktop_theme.txt", "Dark");
        }

        var service = new WorkspaceBackupService(new TestApplicationPaths(fixture.Path));
        WorkspaceRestoreResult result = service.Restore(archivePath);

        Assert.Equal(1, result.Restored);
        Assert.Equal(1, result.Skipped);
        Assert.False(File.Exists(Path.Combine(fixture.Path, "escaped.txt")));
        Assert.False(File.Exists(Path.Combine(fixture.Path, "runtime", "..", "escaped.txt")));
    }

    private static void WriteEntry(ZipArchive archive, string name, string content)
    {
        ZipArchiveEntry entry = archive.CreateEntry(name);
        using Stream stream = entry.Open();
        byte[] bytes = Encoding.UTF8.GetBytes(content);
        stream.Write(bytes, 0, bytes.Length);
    }
}
