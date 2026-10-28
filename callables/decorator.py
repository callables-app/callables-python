import functools
import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from callables.schema import input_model, input_schema, output_kind


GROUP_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
# Commands the CLI already has; a group can't share a name with them (`call <group> <action>`)
RESERVED_GROUP_NAMES = {"check", "local", "manifest", "docs", "init", "run", "help"}


class Group:
    """A set of related actions, like a Typer sub-app: `call <group> <action>` locally, and one section of the
    project in the web app. Secrets listed here are given to every action in the group.

        backups = Group("backups", title="Database backups", secrets=["SPACES_KEY"])

        @backups.deploy(title="Back up now")
        def run(env: Literal["prod", "staging"] = "prod") -> str: ...
    """

    def __init__(self, name: str, *, title: str | None = None, description: str = "",
                 secrets: list[str] | None = None):
        if not GROUP_NAME.match(name):
            raise ValueError(f"Group name '{name}' must be lowercase letters, digits, '-' or '_', starting with a "
                             "letter (it's typed as `call <group> <action>`)")
        if name in RESERVED_GROUP_NAMES:
            raise ValueError(f"'{name}' is a built-in command, so it can't be a group name")
        self.name = name
        self.title = title or name.replace("_", " ").replace("-", " ").capitalize()
        self.description = description
        self.secrets = list(secrets or [])

    def deploy(self, func: Callable[..., Any] | None = None, **options: Any):
        """`@deploy` for an action in this group; takes the same options. Its full name is `<group>.<name>`."""
        return deploy(func, _group=self, **options)

    def command(self, func: Callable[..., Any] | None = None, **options: Any):
        """`@command` for a local-only action in this group: `call <group> <action>`, never deployed."""
        return command(func, _group=self, **options)

    def manifest_entry(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description}

    def __repr__(self) -> str:
        return f"Group({self.name!r})"


class Next:
    """A follow-up action offered on each row (or the single record) of a function's result, with inputs filled in
    from that row. Fields with the same name as the action's inputs are matched automatically; map others by name.

        @users.deploy(title="Find user", next=[make_admin, Next(set_plan, label="Change plan", user_id="id")])
        def search(query: str) -> list[dict]: ...

    `action` is a deployed function, or its name (`"make_admin"`, or `"users.make_admin"` from another group).
    """

    def __init__(self, action: Callable[..., Any] | str, label: str | None = None, **inputs: str):
        self.action = action
        self.label = label
        self.inputs = inputs  # the action's input name -> the result field that fills it

    def resolve(self, source: "DeployedFunction") -> dict[str, Any]:
        """Needs the whole registry, so it runs when the manifest is built, not at decoration time."""
        target = getattr(self.action, "__shipfn__", None)
        if target is None:
            names = [self.action, f"{source.group.name}.{self.action}" if source.group else None]
            target = next((registry[n] for n in names if n in registry), None)
        if target is None:
            raise ValueError(f"next: there's no deployed action named {self.action!r}")
        if target.local_only and not source.local_only:
            raise ValueError(f"next: {target.name} is a local @command, so it isn't deployed and can't be offered here")
        params = list(inspect.signature(target.func).parameters)
        unknown = [name for name in self.inputs if name not in params]
        if unknown:
            raise ValueError(f"next: {target.name} has no input named {', '.join(unknown)}")
        return {"action": target.name, "label": self.label or target.title,
                "inputs": {name: self.inputs.get(name, name) for name in params}}


@dataclass
class DeployedFunction:
    """What @deploy recorded about a function; `registry` maps full names (`group.action` or `action`) to these."""

    func: Callable[..., Any]
    name: str
    title: str
    description: str
    secrets: list[str] = field(default_factory=list)
    schedule: str | None = None
    timeout: int = 300
    group: Group | None = None
    action: str = ""  # the name within its group; the same as name when there's no group
    next: list[Next] = field(default_factory=list)
    local_only: bool = False  # made by @command: runs from the CLI, never deployed

    @property
    def all_secrets(self) -> list[str]:
        """The group's secrets, then the action's own, without repeats."""
        return list(dict.fromkeys([*(self.group.secrets if self.group else []), *self.secrets]))

    @property
    def entrypoint(self) -> str:
        return f"{self.func.__module__}:{self.func.__qualname__}"

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "entrypoint": self.entrypoint,
            "title": self.title,
            "description": self.description,
            "input_schema": input_schema(self.func),
            "output_kind": output_kind(self.func),
            "secrets": self.all_secrets,
            "schedule": self.schedule,
            "timeout": self.timeout,
            "group": self.group.manifest_entry() if self.group else None,
            "action": self.action,
            "next": [n.resolve(self) for n in self.next],
            **({"local_only": True} if self.local_only else {}),
        }

    def call(self, inputs: dict[str, Any]) -> Any:
        """Validate raw JSON inputs against the signature (coercing dates, enums, models) and call."""
        validated = input_model(self.func).model_validate(inputs)
        kwargs = {name: getattr(validated, name) for name in validated.__class__.model_fields}
        return self.func(**kwargs)


# Every @deploy in an imported module lands here; `call manifest` reads it.
registry: dict[str, DeployedFunction] = {}


def deploy(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    secrets: list[str] | None = None,
    schedule: str | None = None,
    timeout: int = 300,
    next: list[Next | Callable[..., Any] | str] | None = None,  # noqa: A002 (reads best as @deploy(next=[...]))
    _group: Group | None = None,
    _local_only: bool = False,
):
    """Deploy a function to Callables: teammates run it from a web form generated from its type hints.

    Works bare (`@deploy`) or with options (`@deploy(title="Weekly report", secrets=["CRM_TOKEN"])`).

    Args:
        name: Identity on the platform (defaults to the function name). Changing it creates a new
            automation, so pass the old name here if you rename the Python function.
        title: Heading people see (defaults to the name as a sentence, e.g. "Weekly report").
        description: One line shown on the card (defaults to the docstring's first paragraph).
        secrets: Environment variable names to inject at run time; values are set in the web app.
            Read them inside the function with `os.environ["NAME"]`.
        schedule: Cron expression in UTC (e.g. "0 9 * * MON"). Every input needs a default.
            Usually unnecessary: anyone can schedule a function from the web app.
        timeout: Seconds before the run is stopped (default 300, platform maximum 900).
        next: Actions people can take on a result row next, with inputs filled from the row: deployed functions,
            their names, or `Next(...)` for a label or field mapping. Builds step-by-step flows.

    Inputs: str, int, float, bool, date, datetime, Literal[...]/Enum (dropdown), list[...], X | None,
    Pydantic models and File (upload). Defaults make a field optional.

    Results by return annotation: str -> Markdown, list[dict] -> table, dict/model -> key-value list,
    File/list[File] -> downloads, None -> "Done".

    To organise related functions, deploy them through a `Group` instead: `@backups.deploy(...)`.
    For a tool that only runs on your machine, use `@command` instead.

    Run `call docs` for the full guide and `call check` before pushing.
    """

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        action = name or f.__name__
        fn_name = f"{_group.name}.{action}" if _group else action
        doc = inspect.getdoc(f) or ""
        deployed = DeployedFunction(
            func=f,
            name=fn_name,
            title=title or action.replace("_", " ").capitalize(),
            description=description or doc.split("\n\n")[0],
            secrets=list(secrets or []),
            schedule=schedule,
            timeout=timeout,
            group=_group,
            action=action,
            next=[n if isinstance(n, Next) else Next(n) for n in (next or [])],
            local_only=_local_only,
        )
        existing = registry.get(fn_name)
        if existing and existing.entrypoint != deployed.entrypoint:
            raise ValueError(
                f"Two functions are deployed as '{fn_name}': {existing.entrypoint} and {deployed.entrypoint}. "
                "Pass name='...' to one of them."
            )
        registry[fn_name] = deployed

        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            return f(*args, **kwargs)

        wrapper.__shipfn__ = deployed
        return wrapper

    return wrap(func) if func is not None else wrap


def command(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    title: str | None = None,
    description: str | None = None,
    next: list[Next | Callable[..., Any] | str] | None = None,  # noqa: A002
    _group: Group | None = None,
):
    """A command for your own machine: `call <group> <action> --flags` (or `call <name>`), built from the type
    hints like a deployed action, but never deployed. For tools that read local files, write reports to disk or open
    a browser, so one project needs one CLI and no separate Typer app.

        @audits.command
        def render(slug: str, open_browser: bool = False) -> str: ...

    Takes `name`, `title`, `description` and `next` like `@deploy`. There are no secrets, schedule or timeout:
    it runs in your own environment.
    """
    return deploy(func, name=name, title=title, description=description, next=next, _group=_group, _local_only=True)
