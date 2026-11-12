"""Pick a model for a new card from per-connection ``model_rules``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from .adapters.base import Issue

logger = logging.getLogger(__name__)


def match_model(issue: Issue, rules: Any, roles_file: str | None = None) -> tuple[str | None, str | None]:
    """First matching rule wins. Returns ``(model, provider)``; either may be None."""
    if not isinstance(rules, list):
        return None, None
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        when = rule.get("when") if isinstance(rule.get("when"), Mapping) else {}
        if not _when_matches(issue, when):
            continue
        provider = _text(rule.get("provider"))
        role = _text(rule.get("role"))
        if role:
            return _resolve_role(role, provider, roles_file)
        return _text(rule.get("model")), provider
    return None, None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _when_matches(issue: Issue, when: Mapping[str, Any]) -> bool:
    if not when:
        return True
    if "label" in when:
        wanted = str(when["label"]).casefold()
        if not any(str(name).casefold() == wanted for name in issue.labels):
            return False
    if "estimate_max" in when:
        try:
            ceiling = float(when["estimate_max"])
        except (TypeError, ValueError):
            return False
        if issue.estimate is None or issue.estimate > ceiling:
            return False
    if "estimate_min" in when:
        try:
            floor = float(when["estimate_min"])
        except (TypeError, ValueError):
            return False
        if issue.estimate is None or issue.estimate < floor:
            return False
    return True


def _resolve_role(role: str, provider: str | None, roles_file: str | None) -> tuple[str | None, str | None]:
    if not roles_file:
        logger.warning("issue-sync: role %r given but roles_file is not set", role)
        return None, None
    try:
        data = _load_yaml(Path(roles_file).expanduser())
    except Exception as exc:
        logger.warning("issue-sync: could not read roles_file %s: %s", roles_file, exc)
        return None, None
    if not isinstance(data, Mapping):
        logger.warning("issue-sync: roles_file %s is not a mapping", roles_file)
        return None, None
    roles = data.get("roles") if isinstance(data.get("roles"), Mapping) else {}
    models = data.get("models") if isinstance(data.get("models"), Mapping) else {}
    model_key = roles.get(role)
    if not model_key:
        logger.warning("issue-sync: unknown role %r", role)
        return None, None
    entry = models.get(model_key)
    if not isinstance(entry, Mapping):
        logger.warning("issue-sync: role %r has no models entry", role)
        return None, None
    if not provider:
        logger.warning("issue-sync: role %r matched but no provider was given", role)
        return None, None
    model_id = entry.get(provider)
    if not model_id:
        logger.warning("issue-sync: role %r has no model for provider %r", role, provider)
        return None, None
    return str(model_id), provider


def _load_yaml(path: Path) -> Any:
    import yaml  # Hermes ships PyYAML; import lazily so the plugin has no install-time dep.
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)
