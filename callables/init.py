"""`call init`: an example automation and instructions for coding agents (Claude Code, Cursor, Codex...).

Safe to re-run: existing files are never overwritten, except the files callables owns (the skill, and the
marked section in AGENTS.md / CLAUDE.md), which are refreshed so agents follow the installed SDK version.
"""
from importlib.resources import files
from pathlib import Path

from callables.manifest import discover_modules

SECTION_START = "<!-- callables:start -->"
SECTION_END = "<!-- callables:end -->"
LEGACY_SECTIONS = [("<!-- shipfn:start -->", "<!-- shipfn:end -->")]  # replaced with the current section

SKILL_FRONTMATTER = """---
name: callables
description: Write, change or debug Callables automations (Python functions decorated with @deploy from the callables package). Use when adding an automation, changing inputs/outputs, handling files, secrets or schedules, or when a Callables deploy fails.
---

"""

AGENTS_SECTION = f"""{SECTION_START}
## Callables automations

Functions decorated with `@deploy` (from `callables`) are deployed on push and run by non-technical teammates as web forms.

- Read the guide before writing or changing one: `.claude/skills/callables/SKILL.md`, or run `call docs`.
- Run `call check` before pushing (exit code 1 means the deploy would fail), and try functions with `call local <name> -i key=value`.
- Don't add `callables` to requirements.txt. The platform installs its own copy.
{SECTION_END}
"""

EXAMPLE = '''from typing import Literal

from callables import deploy


@deploy(title="Say hello")
def say_hello(name: str, tone: Literal["friendly", "formal"] = "friendly") -> str:
    """Writes a short greeting. Replace this with your first real automation."""
    if tone == "formal":
        return f"Good day, **{name}**."
    return f"Hey **{name}**! 👋"
'''


def guide() -> str:
    return files("callables").joinpath("GUIDE.md").read_text(encoding="utf-8")


def _upsert_section(path: Path) -> str:
    """Add or refresh the callables section (replacing one written under the old shipfn name), leaving the rest of the file alone."""
    if not path.exists():
        path.write_text(AGENTS_SECTION, encoding="utf-8")
        return "created"
    text = path.read_text(encoding="utf-8")
    for start_marker, end_marker in [(SECTION_START, SECTION_END), *LEGACY_SECTIONS]:
        start, end = text.find(start_marker), text.find(end_marker)
        if start != -1 and end > start:
            updated = text[:start] + AGENTS_SECTION.rstrip("\n") + text[end + len(end_marker):]
            break
    else:
        updated = text.rstrip("\n") + ("\n\n" if text.strip() else "") + AGENTS_SECTION
    if updated == text:
        return "unchanged"
    path.write_text(updated, encoding="utf-8")
    return "updated"


def init_project(root: Path, example: bool = True, agents: bool = True) -> list[tuple[str, str]]:
    """Returns (path, what happened) for each file touched."""
    root = root.resolve()
    done: list[tuple[str, str]] = []

    if example:
        target = root / "automations.py"
        if discover_modules(root):
            done.append(("automations.py", "skipped (this repo already has @deploy functions)"))
        elif target.exists():
            done.append(("automations.py", "skipped (already exists)"))
        else:
            target.write_text(EXAMPLE, encoding="utf-8")
            done.append(("automations.py", "created"))

        reqs = root / "requirements.txt"
        if not reqs.exists() and not (root / "pyproject.toml").exists():
            reqs.write_text("# Your automations' dependencies. Don't list callables: the platform installs it.\n",
                            encoding="utf-8")
            done.append(("requirements.txt", "created"))

        gitignore = root / ".gitignore"
        lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
        if not any(line.strip().strip("/") == "callables-output" for line in lines):
            with gitignore.open("a", encoding="utf-8") as f:
                if lines and lines[-1].strip():
                    f.write("\n")
                f.write("callables-output/\n")
            done.append((".gitignore", "updated" if lines else "created"))

    if agents:
        skill = root / ".claude" / "skills" / "callables" / "SKILL.md"
        old_skill = root / ".claude" / "skills" / "shipfn" / "SKILL.md"
        if old_skill.exists():  # written by an older version under the old name; the new one replaces it
            old_skill.unlink()
            if not any(old_skill.parent.iterdir()):
                old_skill.parent.rmdir()
            done.append((str(old_skill.relative_to(root)), "removed (renamed to callables)"))
        content = SKILL_FRONTMATTER + guide()
        existed = skill.exists()
        if existed and skill.read_text(encoding="utf-8") == content:
            done.append((str(skill.relative_to(root)), "unchanged"))
        else:
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(content, encoding="utf-8")
            done.append((str(skill.relative_to(root)), "updated" if existed else "created"))

        done.append(("AGENTS.md", _upsert_section(root / "AGENTS.md")))
        if (root / "CLAUDE.md").exists():  # only touch CLAUDE.md if the repo already uses one
            done.append(("CLAUDE.md", _upsert_section(root / "CLAUDE.md")))

    return done
