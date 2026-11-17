"""The plugin ships a guided-setup skill (CORE-635)."""

from __future__ import annotations

from pathlib import Path


def test_setup_skill_is_registered_and_exists(plugin, stub_ctx):
    ctx = stub_ctx({})
    plugin.register(ctx, background=False)
    entry = ctx.skills["setup"]
    path = Path(entry["path"])
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\nname: setup\n")
    assert "hermes issues setup" in text and "--print-only" in text
    assert entry["description"].startswith("Use when")


def test_plugin_loads_without_register_skill(plugin, stub_ctx):
    ctx = stub_ctx({})
    ctx.register_skill = None  # older Hermes: no skill API
    plugin.register(ctx, background=False)
    assert "issues" in ctx.commands
