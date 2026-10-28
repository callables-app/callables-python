"""Which folder of a repo the platform builds. By default the whole repo; a repo can point it at a subfolder,
and then only that folder is built: its requirements.txt / pyproject.toml, apt.txt and .python-version."""
import tomllib
from pathlib import Path, PurePosixPath

CONFIG_FILES = ("callables.toml", "shipfn.toml")  # shipfn.toml is the old name, still read
TOOL_KEYS = ("callables", "shipfn")


class ConfigError(ValueError):
    pass


def _configured_root(repo: Path) -> tuple[object, str] | None:
    for name in CONFIG_FILES:
        config = repo / name
        if config.is_file():
            try:
                data = tomllib.loads(config.read_text())
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{name} isn't valid TOML: {exc}")
            if "root" in data:
                return data["root"], name
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
        except tomllib.TOMLDecodeError:
            return None  # the build reports a broken pyproject.toml when it gets there
        for key in TOOL_KEYS:
            tool = data.get("tool", {}).get(key, {})
            if "root" in tool:
                return tool["root"], f"pyproject.toml [tool.{key}]"
    return None


def build_root(repo: Path) -> tuple[Path, str | None]:
    """The folder to build, and where that setting came from (None when it's the whole repo)."""
    repo = repo.resolve()
    configured = _configured_root(repo)
    if configured is None:
        return repo, None
    value, source = configured
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"root in {source} must be a folder path, like root = \"automations\"")
    rel = PurePosixPath(value.strip())
    if rel.is_absolute() or ".." in rel.parts:
        raise ConfigError(f"root in {source} must be a folder inside the repo, relative to it: got {value!r}")
    folder = (repo / rel).resolve()
    if not folder.is_relative_to(repo):  # e.g. a symlink pointing out of the repo
        raise ConfigError(f"root in {source} points outside the repo: {value!r}")
    if not folder.is_dir():
        raise ConfigError(f"root in {source} is {value!r}, but the repo has no folder there")
    return folder, source
