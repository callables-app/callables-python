# Writing Callables automations

Callables turns Python functions into web forms that non-technical teammates run. A developer decorates a function with `@deploy` and pushes the repo. The platform builds it and shows each function as a form, generated from the type hints, with run history. People can also schedule runs from the web app.

The person running an automation can't see the code, a traceback or a terminal. Everything they see comes from the decorator, the docstring, the parameter names and types, and the return value. Write for them.

## The loop

1. Write or change a function decorated with `@deploy`.
2. `call check` (or `call check --json`) lists every problem a deploy would hit, with a fix for each. Exit code 1 means the deploy would fail.
3. `call local <name> -i key=value --json` runs the function on this machine, with the same input validation as the platform.
4. Push. The platform builds and deploys on push.

Run `call check` before every push. If any module fails to import, nothing is published.

### First-time setup

1. Install the CLI into the project's virtualenv: `pip install callables`. Keep it out of `requirements.txt`, because the platform adds its own copy.
2. The project must be a GitHub repo. Callables deploys a repo and branch, not a local folder. Commit and push it first, with `.env`, key files and data kept out by `.gitignore`.
3. In the web app, open **New project**. Pick the repo and branch (install the GitHub App when asked), then **Connect and deploy**. The first deploy starts right away.
4. Set the secrets your functions declare on the project page. After that, every push to the branch deploys.

## Anatomy

```python
from datetime import date
from typing import Literal

from callables import deploy


@deploy(title="Closed deals", secrets=["CRM_TOKEN"], timeout=120)
def closed_deals(region: Literal["NA", "EMEA", "APAC"] = "NA", since: date | None = None,
                 min_value: int = 0) -> list[dict]:
    """Deals closed in a region, newest first.

    Pulls from the CRM; amounts are in USD.
    """
    ...
```

- `name`: the function's identity on the platform. It defaults to the function name. **Renaming it creates a new automation**, and the old one's history and schedules stay behind. Pass `name="..."` to keep the identity if you rename the Python function.
- `title`: the card and page heading. It defaults to the name, turned into a sentence.
- `description`: the first paragraph of the docstring is the default. Write one plain-language sentence about what it does for the person.
- `secrets`: names of environment variables, which builders set in the web app. Read them with `os.environ["CRM_TOKEN"]`. Never hard-code credentials and never take them as inputs. A declared secret that nobody has set doesn't block the run: the variable is simply missing, and builders see a "missing secrets" warning. For an optional secret, declare it and read it with `os.environ.get(...)`. Each secret is a single value, so a key file such as a `.pem` goes in as the file's contents, and the code reads it from the variable.
- `timeout`: in seconds. The default is 300 and the maximum is 900.
- `schedule`: a cron expression in UTC, for schedules owned by the code. Only use it when the user asks for one. Anyone can add a schedule in the web app, so you usually don't need it.

## Groups: organise related functions

A project (one repo) can hold many functions. Put related ones in a `Group`, like a Typer sub-app. People see them as one section of the project in the web app, and you run them as `call <group> <action>`.

```python
from typing import Literal

from callables import Group

backups = Group("backups", title="Database backups", description="Postgres dumps to object storage",
                secrets=["SPACES_KEY", "SPACES_SECRET"])   # given to every action in the group


@backups.deploy(title="Back up now", secrets=["DATABASE_URL"])
def run(env: Literal["prod", "staging"] = "prod", keep: int = 7) -> str:
    ...


@backups.deploy(title="List backups")
def list_all(days: int = 30) -> list[dict]:
    ...
```

```bash
call backups --help       # the group's actions
call backups run --help   # flags, from the type hints
call backups run --env staging --keep 3
```

- A group name is lowercase letters, digits, `-` or `_`, and can't be a built-in command (`check`, `local`, ...). Define each group once and import it wherever you need it.
- An action's full name is `<group>.<name>`, e.g. `backups.run`. Use that with `call local`.
- A function's name is its identity on the platform. Moving an existing function into a group gives it a new name, so it starts with fresh run history and sharing settings.
- A plain `@deploy` still works. Those functions appear in the project without a section, and run as `call <name> --flags`.

### Local commands

Some tools only make sense on your own machine: they read local files, write reports to disk or open a browser. Write them with `@command` (or `@group.command`) instead of `@deploy`, so the project has one CLI and no separate Typer app:

```python
from callables import Group

audits = Group("audits", title="Audits")


@audits.command
def render(slug: str, open_browser: bool = False) -> str:
    ...
```

- They run as `call audits render --slug x` and with `call local`, with flags from the type hints, like any action. `call check` lists them as "(local only)".
- They're never deployed: the platform doesn't see them and nobody can run them from the web app. `@command` takes `name`, `title`, `description` and `next`, but no `secrets`, `schedule` or `timeout`, because it runs in your own environment (read secrets from `.env`). A deployed action's `next=` can't point at one.
- Their module is still imported when the platform builds, so keep imports that only work locally inside the function.
- Return the result instead of printing it. The CLI prints what you return (a table as aligned columns in a terminal, JSON when piped), so printing it as well shows it twice. Keep prints for progress.
- To deploy one later, change `@command` to `@deploy`. It keeps its name.

## Flows: what to do next with a result

Chain actions into step-by-step flows with `next=`. Each row of a table result (or a single dict/model result) offers the next actions, with their inputs filled in from that row. Every step is still an ordinary action: it can be run on its own, scheduled or shared separately.

```python
from typing import Literal

from callables import Group, Next

users = Group("users", title="Users")


@users.deploy(title="Make admin")
def make_admin(user_id: int) -> str: ...


@users.deploy(title="Subscription requirement")
def set_subscription(user_id: int, required: Literal["Required", "Not required"]) -> str: ...


@users.deploy(title="Find user", next=[make_admin, Next(set_subscription, label="Change subscription", user_id="id")])
def search(query: str) -> list[dict]:
    return [{"id": 42, "user_id": 42, "name": "Ann Lee", "email": "ann@example.com"}]
```

- An action's inputs are filled from result fields with the same name. `Next(action, user_id="id")` maps a field with a different name, and `label=` changes the button text.
- `next` takes deployed functions, or names (`"make_admin"`, or `"users.make_admin"` for another group's action) when the action is defined further down.
- In the web app, each result row gets a button per next action. It opens that action's form with the row's values filled in, and the new run links back to the one it came from.
- In a terminal, `call users search --query ann` lists the rows, asks which one and what to do next, prompts for the inputs the row didn't fill, and runs it. Press Enter to stop. Scripts and `--json` get plain output with no prompts.
- People only see next actions they're allowed to run.

## Inputs: type hints become the form

| Type hint | Form field |
|---|---|
| `str` | text box |
| `int`, `float` | number |
| `bool` | checkbox |
| `date`, `datetime` | date or date-time picker |
| `Literal["a", "b"]` or an `Enum` | dropdown |
| `list[str]` and so on | list editor |
| `X \| None = None` | optional field |
| a Pydantic model | grouped sub-form |
| `File`, or `Annotated[File, accept(".csv")]` | file upload |

- A default value makes the field optional and prefills it. A parameter without a default is required.
- Parameter names become labels: `min_value` shows as "Min value". Choose names that read well.
- Prefer `Literal[...]` over free text whenever the valid values are known.
- Not supported: `*args`, `**kwargs`, `Callable` or arbitrary classes. `call check` names the offending parameter.
- Avoid `dict` inputs. They show as a raw JSON box, so use a Pydantic model instead.
- Inputs are validated and coerced before your function runs. A `date` arrives as a `datetime.date`, and a model arrives as an instance.

## Results: the return type picks the display

| Return annotation | Shown as |
|---|---|
| `-> str` | Markdown (headings, lists, links and tables work) |
| `-> list[dict]`, a list of models, or a DataFrame | a table with CSV download |
| `-> dict` or a Pydantic model | a key-value list |
| `-> File` or `-> list[File]` | download buttons |
| `-> None` | "Done" |
| missing or anything else | raw JSON (avoid this) |

Always annotate the return type. Keep table rows flat (scalar values) and use readable keys, because the keys become column headers.

## Files

```python
from typing import Annotated

from callables import File, accept, deploy, save_file


@deploy(title="Clean up a contact list")
def clean_contacts(sheet: Annotated[File, accept(".csv")]) -> File:
    """Removes duplicate rows and fixes capitalisation."""
    rows = sheet.read_text().splitlines()            # also: .read_bytes(), .open(), .path, .name, .size
    save_file(f"{len(rows)} rows in".encode(), name="summary.txt")   # an extra download
    out = "/tmp/clean.csv"
    ...
    return File(out, name=f"clean-{sheet.name}")
```

- A `File` parameter is an upload field. Use `accept(...)` to limit the file types.
- Returning `File` gives the person a download. `save_file(path_or_bytes, name=...)` adds more downloads alongside any return value. It needs `name=` when you pass bytes.
- Locally, pass a file with `-i sheet=@path/to/file.csv`. Outputs land in `./callables-output/`.
- Limits: 100 MB per upload, and 100 MB and 20 files of output per run. Files are deleted after 24 hours.
- Functions with file inputs can't be scheduled.

## Runtime environment

- Each run gets a fresh sandbox on Debian 13 (trixie). It runs as a non-root user and has internet access.
- **Python version:** taken from `.python-version` (e.g. `3.13`), else the newest version your `pyproject.toml` `requires-python` allows, else 3.12. Supported: 3.11 to 3.14.
- **Python dependencies** come from `requirements.txt` at the repo root, or from `[project].dependencies` in `pyproject.toml`. **Don't list `callables`.** The platform installs its own matching copy.
- **Automations in a subfolder:** by default the whole repo is built. To build only one folder, add `callables.toml` at the repo root containing `root = "automations"` (or `[tool.callables] root = "automations"` in `pyproject.toml`). Then only that folder goes into the image, and its own `requirements.txt`, `apt.txt` and `.python-version` are used. Code in it can't import from the rest of the repo. `call check`, `local` and `manifest` use the same folder when run from the repo root.
- **Writable space:** your home directory (`~`), and `/tmp`, which is limited to 256 MB. Your code in `/app` is read-only. Every run starts fresh, so nothing persists between runs. Keep state in your own database or storage.
- **Running downloaded binaries:** `/tmp` doesn't allow running programs, so a binary downloaded there fails with `Permission denied`. Put it under `~` and `chmod +x` it. Better still, install the tool with `apt.txt` (below).
- Every module that imports `callables` and mentions `deploy` is imported at build time. Keep import-time code cheap and free of side effects: no network calls, and no reading secrets at module level. Read secrets inside the function.
- `print()` output and `logging` show as the run's logs. Tracebacks are shown only to builders, never to the person running the automation.

## System packages

When a function shells out to a command-line tool (a converter, a database client, a headless browser), list the Debian packages it needs in `apt.txt` at the repo root:

```
# apt.txt: one package per line
ffmpeg
poppler-utils
```

- Names only, no versions. You get the version Debian 13 (trixie) ships, which stays the same apart from security fixes. If the tool talks to an outside service, check that this version works with it.
- Packages are installed without their "recommended" extras. If a tool is missing something (fonts, for example), list that package too.
- They're installed once per change to `apt.txt`, not on every push.
- `call local` uses the tools on your own machine, so install them there too to test.

## Memory and CPU

You don't size anything. A run can use memory and CPU up to your organisation's plan limit, and usage is billed on what the run actually used, not on the limit. Builders see each run's peak memory and CPU time on its page.

If a run goes over the memory limit, it fails with an error that says so. Fix it by using less memory (process files in chunks, stream instead of loading everything, free large objects you're done with), or move to a bigger plan. Child processes count toward the same limit.

## Errors people will see

Raise exceptions with messages written for the person running the automation. They see `ValueError: No deals found for EMEA since 2026-01-01`, so make it say what to change. For bad input, raise `ValueError` with a message about that field.

## Commands

- `call check [--json]`: problems a deploy would hit, with fixes. Exit code 1 on errors.
- `call <group> <action> [--flags]` (or `call <function> [--flags]`): run a function like a CLI command. Each input is a flag (`days` becomes `--days`, a bool becomes `--dry-run/--no-dry-run`, a list takes several values). Add `--help` to see them.
- `call local <name> [-i key=value ...] [--json]`: run a function on this machine. Values are parsed as JSON when possible, and `@path` passes a file. `--json` prints one JSON object on stdout (`ok`, `value`, `files`, `error`, `field_errors`), and the function's own prints go to stderr.
- `call manifest`: the full JSON the platform reads, including each input's JSON Schema. Local `@command`s are left out.
- `call docs`: prints this guide.
- `call init`: an example automation plus agent instructions (`.claude/skills/callables/SKILL.md` and an `AGENTS.md` section). Run it again after upgrading the SDK to refresh the instructions.
