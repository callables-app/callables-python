"""Which folder of a repo the platform builds. By default the whole repo; a repo can point it at a subfolder,
and then only that folder is built: its requirements.txt / pyproject.toml, apt.txt and .python-version.
A project's own folder setting wins over the repo's config, so one repo can back several projects."""
import tomllib
from pathlib import Path, PurePosixPath

CONFIG_FILES = ("callables.toml",)
TOOL_KEYS = ("callables",)


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


def build_root(repo: Path, project_root: str | None = None) -> tuple[Path, str | None]:
    """The folder to build, and where that setting came from (None when it's the whole repo). `project_root` is the
    project's folder setting, relative to the repo."""
    repo = repo.resolve()
    configured = (project_root, "the project's settings") if project_root else _configured_root(repo)
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


# ── include: sibling folders a subfolder build pulls in ─────────────────────
# A folder built on its own (root = "recipes/seo") can import code from elsewhere in the repo by listing those
# folders in its own callables.toml: include = ["google/search_console", "umami"]. The build then lays out the
# folder and its includes at their repo paths, so `from umami.umami_api import ...` works exactly as it does when
# the whole repo is built. Dependencies, apt.txt and .python-version still come from the folder itself.

def _configured_includes(folder: Path) -> tuple[object, str] | None:
    for name in CONFIG_FILES:
        config = folder / name
        if config.is_file():
            try:
                data = tomllib.loads(config.read_text())
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{name} isn't valid TOML: {exc}")
            if "include" in data:
                return data["include"], name
    pyproject = folder / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
        except tomllib.TOMLDecodeError:
            return None
        for key in TOOL_KEYS:
            tool = data.get("tool", {}).get(key, {})
            if "include" in tool:
                return tool["include"], f"pyproject.toml [tool.{key}]"
    return None


def includes(repo: Path, folder: Path) -> list[Path]:
    """The folders `folder` includes, as absolute paths inside `repo`. Empty when it includes nothing or when it's
    the whole repo (everything is already in the build)."""
    repo, folder = repo.resolve(), folder.resolve()
    configured = _configured_includes(folder)
    if configured is None or folder == repo:
        return []
    value, source = configured
    where = f"include in {folder.relative_to(repo).as_posix()}/{source}"
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{where} must be a list of folders, like include = [\"shared\", \"google/gmail\"]")
    out: list[Path] = []
    for raw in value:
        rel = PurePosixPath(raw.strip())
        if rel.is_absolute() or ".." in rel.parts:
            raise ConfigError(f"{where}: folders are relative to the top of the repo, got {raw!r}")
        path = (repo / rel).resolve()
        if not path.is_relative_to(repo):
            raise ConfigError(f"{where}: {raw!r} points outside the repo")
        if not path.is_dir():
            raise ConfigError(f"{where}: the repo has no folder {raw!r} (paths start at the top of the repo)")
        if path == repo or folder.is_relative_to(path):
            raise ConfigError(f"{where}: {raw!r} contains the folder being built; include only the folders it imports")
        if path.is_relative_to(folder):
            raise ConfigError(f"{where}: {raw!r} is already inside the folder being built")
        out.append(path)
    # A folder inside another included folder is already there
    return [p for p in dict.fromkeys(out) if not any(p != q and p.is_relative_to(q) for q in out)]


def find_repo(folder: Path) -> Path | None:
    """The git repo holding `folder`, if any."""
    folder = folder.resolve()
    for parent in [folder, *folder.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def repo_for(folder: Path) -> Path | None:
    """The repo a folder belongs to: its git repo, else the folder you run from when the folder is inside it (an
    unzipped upload has no .git)."""
    folder = folder.resolve()
    found = find_repo(folder)
    if found:
        return found
    cwd = Path.cwd().resolve()
    return cwd if folder != cwd and folder.is_relative_to(cwd) else None


def layout(folder: Path, repo: Path | None = None) -> tuple[Path, list[Path]]:
    """Where to import from and which folders to load callables from, for building `folder`: the folder itself, or,
    when it includes other folders, the repo top (so modules keep their repo paths) and the folder plus its
    includes. `repo` defaults to repo_for(folder); with no repo there's nothing to include from."""
    folder = folder.resolve()
    repo = repo.resolve() if repo else repo_for(folder)
    found = includes(repo, folder) if repo else []
    if not found:
        return folder, [folder]
    return repo, [folder, *found]
