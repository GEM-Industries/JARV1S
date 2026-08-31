import importlib
import inspect
import pkgutil
from typing import Any, Dict, List, get_args, get_origin

from core.plugins.types import JarvisPlugin
from core.config import settings


ALLOWED_LOOSE_TOOL_RETURNS = {
    "agents.list_tasks",  # Broad task inspection should not hide stored task fields.
    "system.diagnostics",  # Bounded human-readable key/value diagnostics.
}


def _is_loose_return(annotation) -> bool:
    if annotation is inspect.Signature.empty:
        return False
    if annotation is Any:
        return True
    origin = get_origin(annotation)
    args = get_args(annotation)
    if annotation in {dict, Dict} or origin in {dict, Dict}:
        return True
    if annotation in {list, List} or origin in {list, List}:
        return not args or get_origin(args[0]) in {dict, Dict} or args[0] in {dict, Dict, Any}
    return False


def _plugin_classes() -> list[type[JarvisPlugin]]:
    classes: list[type[JarvisPlugin]] = []
    for _, name, _ in pkgutil.iter_modules([str(settings.PLUGINS_DIR)]):
        module = importlib.import_module(f"plugins.{name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, JarvisPlugin) and obj is not JarvisPlugin:
                classes.append(obj)
    return classes


def test_public_tools_do_not_add_new_loose_return_shapes():
    loose_tools: set[str] = set()

    for plugin_cls in _plugin_classes():
        plugin = plugin_cls()
        for tool_name, func in plugin.get_tools().items():
            annotation = inspect.signature(func).return_annotation
            if _is_loose_return(annotation):
                loose_tools.add(f"{plugin.name}.{tool_name}")

    assert loose_tools <= ALLOWED_LOOSE_TOOL_RETURNS
