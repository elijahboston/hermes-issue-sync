"""Jira Cloud adapter: REST v3 with Basic auth (account email + API token).

Credentials are read through ``get_secret`` on every request, because the Hermes secret scope is
per thread. Jira cannot set a status directly: an issue moves through a *transition*, so
``move_issue`` looks up the issue's available transitions and runs the one whose target status
matches. Descriptions and comments are Atlassian Document Format (ADF), converted by ``adf``.
Search uses the current ``POST /rest/api/3/search/jql`` endpoint (token pagination).
"""

from __future__ import annotations

import base64
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ..http import JsonClient, Transport
from ..secrets import get_secret
from ..signature import is_signed
from . import adf
from .base import AdapterError, Comment, Issue, TrackerAdapter, TransitionNotAllowed, filter_projects

DEFAULT_BASE_URL_ENV = "JIRA_BASE_URL"
DEFAULT_EMAIL_ENV = "JIRA_EMAIL"
DEFAULT_TOKEN_ENV = "JIRA_API_TOKEN"
DEFAULT_LABEL = "agent"
PAGE_SIZE = 50
COMMENT_PAGE_SIZE = 100
SEARCH_FIELDS = ["summary", "description", "status", "labels", "updated"]

_TZ_NO_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def _parse_time(value: str) -> datetime:
    """Jira writes ``2026-11-07T05:41:08.540+0000``; also accept ``Z`` and ``+00:00``."""
    text = value.strip().replace("Z", "+00:00")
    text = _TZ_NO_COLON.sub(r"\1:\2", text)
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _jql_string(value: str) -> str:
    """Quote a value for JQL: wrap in double quotes, escape backslashes and quotes."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_jql(filter: Mapping[str, Any]) -> str:
    """JQL for the plugin's ``filter`` setting: raw ``jql`` wins, else ``project`` AND ``label``."""
    filter = filter or {}
    raw = str(filter.get("jql") or "").strip()
    if raw:
        return raw
    clauses = []
    projects = filter_projects(filter)
    if len(projects) == 1:
        clauses.append(f"project = {_jql_string(projects[0])}")
    elif projects:
        clauses.append("project in (" + ", ".join(_jql_string(p) for p in projects) + ")")
    clauses.append(f"labels = {_jql_string(filter.get('label') or DEFAULT_LABEL)}")
    return " AND ".join(clauses) + " ORDER BY created ASC"


class JiraAdapter(TrackerAdapter):
    def __init__(self, settings: Mapping[str, Any], *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._settings = dict(settings or {})
        self._base_env = str(self._settings.get("base_url_env") or DEFAULT_BASE_URL_ENV)
        self._email_env = str(self._settings.get("email_env") or DEFAULT_EMAIL_ENV)
        self._token_env = str(self._settings.get("token_env") or DEFAULT_TOKEN_ENV)
        self._client = JsonClient("jira", transport, clock=clock)
        self._account_id: str | None = None

    @property
    def name(self) -> str:
        return "jira"

    # -- transport ---------------------------------------------------------------------------

    def _base_url(self) -> str:
        base = (get_secret(self._base_env) or "").strip().rstrip("/")
        if not base:
            raise AdapterError(f"jira: {self._base_env} is not set")
        if not base.startswith("https://"):
            raise AdapterError(f"jira: {self._base_env} must start with https:// (e.g. https://example.atlassian.net)")
        return base

    def _auth_header(self) -> str:
        email = (get_secret(self._email_env) or "").strip()
        token = (get_secret(self._token_env) or "").strip()
        missing = [name for name, value in ((self._email_env, email), (self._token_env, token)) if not value]
        if missing:
            raise AdapterError(f"jira: {', '.join(missing)} not set")
        return "Basic " + base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")

    def _request(self, method: str, path: str, *, json_body: Any = None, query: Mapping[str, Any] | None = None,
                 allow_statuses: tuple[int, ...] = ()):
        url = self._base_url() + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        return self._client.request(method, url, json_body=json_body, headers={"Authorization": self._auth_header()},
                                    allow_statuses=allow_statuses)

    @staticmethod
    def _issue_path(key: str, suffix: str = "") -> str:
        return f"/rest/api/3/issue/{urllib.parse.quote(key, safe='')}{suffix}"

    # -- lookups -----------------------------------------------------------------------------

    def _myself(self) -> str:
        if self._account_id is None:
            data = self._request("GET", "/rest/api/3/myself").data or {}
            self._account_id = str(data.get("accountId") or "")
        return self._account_id

    def _current_status(self, key: str) -> str:
        resp = self._request("GET", self._issue_path(key), query={"fields": "status"}, allow_statuses=(404,))
        if resp.status == 404:
            raise AdapterError(f"jira: issue {key} not found")
        return str((((resp.data or {}).get("fields") or {}).get("status") or {}).get("name") or "")

    def _comment(self, node: Mapping[str, Any]) -> Comment:
        author = node.get("author") or {}
        body = adf.to_text(node.get("body"))
        return Comment(
            id=str(node.get("id") or ""),
            author=str(author.get("displayName") or author.get("emailAddress") or "unknown"),
            body=body,
            created_at=node.get("created"),
            is_bot=is_signed(body),
        )

    # -- TrackerAdapter ----------------------------------------------------------------------

    def fetch_issues(self, filter: Mapping[str, Any]) -> list[Issue]:
        jql = build_jql(filter)
        base = self._base_url()
        issues: list[Issue] = []
        token: str | None = None
        while True:
            body: dict[str, Any] = {"jql": jql, "fields": SEARCH_FIELDS, "maxResults": PAGE_SIZE}
            if token:
                body["nextPageToken"] = token
            data = self._request("POST", "/rest/api/3/search/jql", json_body=body).data or {}
            for node in data.get("issues") or []:
                fields = node.get("fields") or {}
                key = str(node.get("key") or "")
                if not key:
                    continue
                issues.append(Issue(
                    key=key,
                    title=str(fields.get("summary") or ""),
                    body=adf.to_text(fields.get("description")),
                    url=f"{base}/browse/{key}",
                    state=str((fields.get("status") or {}).get("name") or ""),
                    labels=tuple(str(label) for label in fields.get("labels") or []),
                    updated_at=fields.get("updated"),
                    # Jira's "done" category covers Done, Closed, Won't Do and any custom final status.
                    closed=((fields.get("status") or {}).get("statusCategory") or {}).get("key") == "done",
                ))
            token = data.get("nextPageToken")
            if data.get("isLast", True) or not token:
                return issues

    def move_issue(self, key: str, target_state: str) -> None:
        wanted = target_state.strip().casefold()
        if self._current_status(key).casefold() == wanted:
            return
        data = self._request("GET", self._issue_path(key, "/transitions")).data or {}
        transitions = list(data.get("transitions") or [])
        chosen = next((t for t in transitions if ((t.get("to") or {}).get("name") or "").casefold() == wanted), None)
        if chosen is None:
            chosen = next((t for t in transitions if (t.get("name") or "").casefold() == wanted), None)
        if chosen is None:
            reachable = sorted({(t.get("to") or {}).get("name") or t.get("name") or "?" for t in transitions})
            raise TransitionNotAllowed(
                f"jira: {key} cannot move to {target_state!r}; reachable statuses: {', '.join(reachable) or 'none'}")
        self._request("POST", self._issue_path(key, "/transitions"), json_body={"transition": {"id": str(chosen["id"])}})

    def post_comment(self, key: str, body: str) -> Comment:
        data = self._request("POST", self._issue_path(key, "/comment"), json_body={"body": adf.from_text(body)}).data
        if not isinstance(data, dict) or not data.get("id"):
            raise AdapterError(f"jira: commenting on {key} was not accepted")
        comment = self._comment(data)
        return Comment(id=comment.id, author=comment.author, body=comment.body or body,
                       created_at=comment.created_at, is_bot=True)

    def list_comments_since(self, key: str, cursor: str | None) -> tuple[list[Comment], str | None]:
        since = _parse_time(cursor) if cursor else None
        nodes: list[dict[str, Any]] = []
        start = 0
        while True:
            data = self._request("GET", self._issue_path(key, "/comment"), query={
                "orderBy": "created", "startAt": start, "maxResults": COMMENT_PAGE_SIZE}).data or {}
            page = list(data.get("comments") or [])
            nodes.extend(page)
            start += len(page)
            if not page or start >= int(data.get("total") or 0):
                break
        fresh = [(_parse_time(n["created"]), n) for n in nodes if n.get("created")]
        fresh = sorted((p for p in fresh if since is None or p[0] > since), key=lambda p: p[0])
        if not fresh:
            return [], cursor
        comments = [self._comment(node) for _, node in fresh]
        return comments, comments[-1].created_at

    def check(self) -> None:
        if not self._myself():
            raise AdapterError("jira: could not identify the API token's account; check JIRA_EMAIL and JIRA_API_TOKEN")
        for project in filter_projects(self._settings.get("filter")):
            path = f"/rest/api/3/project/{urllib.parse.quote(project, safe='')}"
            if self._request("GET", path, allow_statuses=(404,)).status == 404:
                raise AdapterError(f"jira: project {project!r} not found (or no permission to browse it)")
