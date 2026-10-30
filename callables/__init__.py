"""Callables SDK: mark functions with @deploy and push the repo; the platform does the rest."""
from callables.decorator import DeployedFunction, Group, Next, command, deploy, registry
from callables.files import File, accept, save_file

__version__ = "0.2.0"
__all__ = ["deploy", "command", "Group", "Next", "DeployedFunction", "registry", "File", "accept", "save_file", "__version__"]
