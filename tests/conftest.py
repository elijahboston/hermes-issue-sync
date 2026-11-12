"""Shared fixtures: load the repo root as the plugin package and a stub Hermes ``ctx``."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "hermes_issue_sync"


def load_plugin():
    """Import the repo-root ``__init__.py`` as package ``hermes_issue_sync`` (as Hermes would)."""
    module = sys.modules.get(PACKAGE)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        PACKAGE, REPO_ROOT / "__init__.py", submodule_search_locations=[str(REPO_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[PACKAGE]
        raise
    return module


# The repo root holds the plugin's ``__init__.py``, so pytest collects it as a package and, its
# directory name not being an identifier, imports it as top-level ``__init__``, where the
# relative imports fail. Hand pytest the properly loaded package under that name instead.
sys.modules.setdefault("__init__", load_plugin())


class StubCtx:
    """The documented plugin ``ctx`` surface, recording registrations."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        load_plugin()
        # The plugin's own module, so isinstance/identity matches what register() uses.
        memory_store = importlib.import_module(f"{PACKAGE}.issue_sync.state").MemoryStore
        self.config = dict(config or {})
        self.hooks: dict[str, Callable[..., Any]] = {}
        self.commands: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {}
        self.cli_commands: dict[str, dict[str, Any]] = {}
        self.state = memory_store()
        self.profile_name = "default"

    def get_config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default)

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        self.hooks[name] = callback

    def register_command(self, name: str, handler: Callable[..., Any], **kwargs: Any) -> None:
        self.commands[name] = (handler, kwargs)

    def register_cli_command(self, name: str, help: str, setup_fn: Callable[..., Any],
                             handler_fn: Callable[..., Any] | None = None,
                             description: str = "") -> None:
        self.cli_commands[name] = dict(help=help, setup_fn=setup_fn, handler_fn=handler_fn,
                                       description=description)


@pytest.fixture
def plugin():
    return load_plugin()


@pytest.fixture
def stub_ctx() -> type[StubCtx]:
    return StubCtx
