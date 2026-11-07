"""`.env` for local runs. `call local` and `call <group> <action>` load the project's `.env` so a function finds the
secrets it would get on the platform. Variables already set win, and the platform (`call run`) never reads it."""
import os
from pathlib import Path

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(value: str) -> str:
    out, chars = [], iter(value)
    for ch in chars:
        if ch == "\\":
            nxt = next(chars, "")
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
        else:
            out.append(ch)
    return "".join(out)


def parse(text: str) -> dict[str, str]:
    """KEY=value lines. Blank lines and `#` comments are skipped, `export ` is allowed, values may be quoted
    ('single' kept as is, "double" with \\n-style escapes), and an unquoted value ends at ` #`. No ${VAR} expansion."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = _unescape(value[1:-1]) if value[0] == '"' else value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load(*folders: Path) -> tuple[list[Path], list[str]]:
    """Loads `.env` from each folder, earlier folders first, without overriding variables already set.
    Returns the files read and the names it set."""
    files, added = [], []
    for folder in dict.fromkeys(Path(f).resolve() for f in folders):
        path = folder / ".env"
        if not path.is_file():
            continue
        files.append(path)
        for key, value in parse(path.read_text(encoding="utf-8")).items():
            if key not in os.environ:
                os.environ[key] = value
                added.append(key)
    return files, added
