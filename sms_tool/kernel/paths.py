import os
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Environment override for the directory that holds credential-bearing local
# files (config shards, mailbox pool). The WPF shell sets it for every backend
# process it starts so both sides always agree on the location.
CONFIG_DIR_ENV = "GPT_REGISTER_CONFIG_DIR"

# Files that used to live in the repository root. They are moved (never copied
# and left behind) into the config directory the first time it is resolved.
LEGACY_ROOT_FILES = (
    "config.json",
    "proxy.json",
    "runtime.json",
    "payment.json",
    "mailbox_tokens.txt",
)


def project_path(value, default="."):
    raw = str(value or default).strip() or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def output_dir(cfg):
    return project_path((cfg.get("output") or {}).get("directory"), "sessions")


def runtime_dir(cfg):
    return project_path((cfg.get("runtime") or {}).get("directory"), "runtime")


def runtime_file(cfg, filename):
    directory = runtime_dir(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / filename


def _platform_config_base() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def default_user_config_dir() -> Path:
    """Per-user location that is never inside a git checkout."""
    return _platform_config_base() / "GPTRegisterTool" / "config"


def config_dir() -> Path:
    """Directory holding config shards and the default mailbox pool.

    ``GPT_REGISTER_CONFIG_DIR`` wins when set (tests, portable installs, the
    desktop shell). Otherwise it is a per-user directory outside the repository,
    so ``git add -A``, ``git clean`` and a re-clone can neither publish nor
    destroy credentials.
    """
    override = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return default_user_config_dir()


def _move_file(source: Path, target: Path) -> None:
    """Move across volumes without ever exposing a half-written target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.migrating.{os.getpid()}")
    try:
        shutil.copy2(source, staging)
        if staging.stat().st_size != source.stat().st_size:
            raise OSError(f"size mismatch while migrating {source.name}")
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
    source.unlink()


def migrate_legacy_root_files(
    destination: Path | None = None,
    *,
    source: Path = PROJECT_ROOT,
) -> list[str]:
    """Move credential files from the repository root into ``destination``.

    A file is moved only when the destination does not already have it, so an
    existing new-location file always wins and a re-run is a no-op. The moved
    names are returned; a failure on one file never blocks the others (the file
    simply stays where it was and the caller keeps reading the legacy path).
    """
    target_dir = Path(destination) if destination is not None else config_dir()
    try:
        if source.resolve() == target_dir.resolve():
            return []
    except OSError:
        return []
    moved: list[str] = []
    for name in LEGACY_ROOT_FILES:
        for candidate in (name, name + ".bak"):
            legacy = source / candidate
            target = target_dir / candidate
            if not legacy.is_file() or target.exists():
                continue
            try:
                _move_file(legacy, target)
            except OSError as exc:
                sys.stderr.write(f"[!] could not migrate {candidate} to {target_dir}: {exc}\n")
                continue
            moved.append(candidate)
    return moved


def migrate_legacy_root_files_if_default() -> list[str]:
    """Run the migration only for the implicit per-user directory.

    An explicit ``GPT_REGISTER_CONFIG_DIR`` is an operator decision (tests, a
    portable install, the desktop shell which migrates on its own), so it must
    never pull files out of the repository root behind their back.
    """
    if os.environ.get(CONFIG_DIR_ENV, "").strip():
        return []
    return migrate_legacy_root_files(default_user_config_dir())


def resolve_config_file(value, default_name: str = "mailbox_tokens.txt") -> Path:
    """Resolve a user-configured credential file path (for example the mailbox pool).

    Absolute paths are used verbatim. A relative path means "inside the config
    directory"; the legacy repository-root / working-directory file is still
    honoured while it exists and has not been migrated yet.
    """
    raw = str(value or "").strip() or default_name
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    preferred = config_dir() / path
    if preferred.exists() or os.environ.get(CONFIG_DIR_ENV, "").strip():
        return preferred
    for legacy in (PROJECT_ROOT / path, Path.cwd() / path):
        if legacy.exists():
            return legacy
    return preferred
