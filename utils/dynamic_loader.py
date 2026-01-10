"""
Dynamic Loader Utility

Load a class from a dotted module path or a filesystem path.

Examples:
- load_class("strategies.arbitrage.strategy:ArbitrageStrategy")
- load_class("strategies/custom_strategy.py:CustomStrategy")
- load_class("strategies.extreme_price.strategy")  # defaults to class name 'Strategy'
"""
import importlib
import importlib.util
import os
from types import ModuleType
from typing import Any, Type


def _load_module_from_file(file_path: str) -> ModuleType:
    module_name = os.path.splitext(os.path.basename(file_path))[0]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from file: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def load_class(path: str, default_class_name: str = "Strategy") -> Type[Any]:
    """Load and return a class given a path string.

    The path can be one of:
    - "dotted.module.path:ClassName"
    - "relative/or/absolute/file.py:ClassName"
    - "dotted.module.path" (defaults to 'Strategy' as class name)
    - "relative/or/absolute/file.py" (defaults to 'Strategy')
    """
    if not path:
        raise ValueError("Path required to load class")

    if ":" in path:
        module_part, class_name = path.split(":", 1)
    else:
        module_part, class_name = path, default_class_name

    # Decide if it's a file path
    is_file = module_part.endswith(".py") or os.path.sep in module_part

    if is_file:
        module = _load_module_from_file(os.path.abspath(module_part))
    else:
        module = importlib.import_module(module_part)

    try:
        cls = getattr(module, class_name)
    except AttributeError as e:
        raise ImportError(f"Class '{class_name}' not found in '{module_part}'") from e

    if not isinstance(cls, type):
        raise TypeError(f"Loaded object '{class_name}' is not a class")
    return cls
