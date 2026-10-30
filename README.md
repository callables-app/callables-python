# Callables Python SDK

Turn Python functions into forms, schedules and run history your whole team can use. Decorate a function with `@deploy`, push the repo, and people run it from the Callables web app — no Python needed on their end.

```bash
pip install callables
```

Docs: [callables.app/docs](https://callables.app/docs).

This installs the `call` command.

```python
from typing import Literal
from callables import deploy

@deploy(title="Weekly report", secrets=["SALES_API_TOKEN"], schedule="0 9 * * MON")
def weekly_report(region: Literal["NA", "EMEA", "APAC"] = "NA", days: int = 7) -> list[dict]:
    """Deals closed in the last N days."""
    ...
```

- **Inputs** come from the type hints: `str`, `int`, `float`, `bool`, `date`, `Literal[...]`/`Enum` (dropdowns), `list[...]`, optional (`X | None`) and Pydantic models. Defaults become prefilled values.
- **Results** are shown by return type: `str` as Markdown, `list[dict]` (or a DataFrame) as a table with CSV download, `dict`/Pydantic model as a key-value list, `None` as "done".
- **Files in**: a `File` parameter becomes an upload field. Your function gets a `File` with `.path`, `.name`, `.read_text()`, `.read_bytes()` and `.open()`. Limit the picker with `Annotated[File, accept(".csv", ".xlsx")]`.
- **Files out**: return a `File` (or a `list[File]`) and people get download buttons. To hand back files alongside another result, call `save_file(path_or_bytes, name=...)`. Files are kept for 24 hours by default.
- **Secrets** you list are injected as environment variables at run time (`os.environ["SALES_API_TOKEN"]`); set their values in the Callables console.
- **Schedules** are cron expressions, in UTC.

```python
from typing import Annotated
from callables import deploy, File, accept, save_file

@deploy(title="Clean up a sales sheet")
def clean(sheet: Annotated[File, accept(".csv")]) -> File:
    rows = sheet.read_text().splitlines()
    save_file(f"{len(rows)} rows".encode(), name="summary.txt")   # an extra download
    out = "/tmp/clean.csv"
    ...
    return File(out, name=f"clean-{sheet.name}")
```

Try a function on your machine before pushing:

```bash
call check                                   # problems a deploy would hit, with fixes (exit 1 on errors)
call weekly_report --region EMEA --days 30   # every function is also a CLI command
call local weekly_report -i region=EMEA -i days=30
call local clean -i sheet=@sales.csv         # @path passes a file; outputs land in ./callables-output/
call manifest                                # what the platform will see
```

## Coding agents

The SDK carries its own guide for Claude Code, Cursor, Codex and other agents:

```bash
call init   # an example automation, .claude/skills/callables/SKILL.md and an AGENTS.md section
call docs   # print the full guide
```

`call check --json` and `call local ... --json` print one JSON object on stdout, so an agent can check and run functions and read the result. Re-run `call init` after upgrading the SDK to refresh the instructions. Your own files and the rest of AGENTS.md are left alone.

On the platform, builds use the server's own copy of this package, so you don't need to add `callables` to your `requirements.txt` (it's fine if you do).

## Developing the SDK

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The platform (callables-api) depends on this package and copies the installed version into every build, so a release only reaches deployed functions once the API is updated to it. See [RELEASING.md](RELEASING.md).

## License

Apache-2.0. See [LICENSE](LICENSE). Copyright 2026 Nomial. "Callables" is a trademark of Nomial and is not covered by the license.
