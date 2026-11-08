"""Linear adapter: GraphQL over ``https://api.linear.app/graphql`` with a personal API key.

The key is sent raw in ``Authorization`` (Linear personal keys take no ``Bearer`` prefix) and is
resolved through ``get_secret`` on every request, because the Hermes secret scope is per thread.
Linear reports most failures as GraphQL ``errors`` (HTTP 400, sometimes 200); rate limiting is
``extensions.code == "RATELIMITED"`` with ``x-ratelimit-*-reset`` epoch-millisecond headers.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ..http import JsonClient, Transport, excerpt, retry_after
from ..secrets import get_secret
from ..signature import is_signed
from .base import AdapterError, Comment, Issue, RateLimited, TrackerAdapter, TransitionNotAllowed

API_URL = "https://api.linear.app/graphql"
DEFAULT_KEY_ENV = "LINEAR_API_KEY"
DEFAULT_LABEL = "agent"
PAGE_SIZE = 50

ISSUES_QUERY = """
query Issues($filter: IssueFilter!, $first: Int!, $after: String) {
  issues(filter: $filter, first: $first, after: $after) {
    nodes {
      identifier title description url updatedAt
      state { name }
      labels(first: 100) { nodes { name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

ISSUE_REF_QUERY = """
query IssueRef($id: String!) {
  issue(id: $id) { id state { id name } team { id } }
}
"""

TEAM_STATES_QUERY = """
query TeamStates($id: String!, $first: Int!, $after: String) {
  team(id: $id) {
    states(first: $first, after: $after) {
      nodes { id name type position }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

ISSUE_UPDATE_MUTATION = """
mutation MoveIssue($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) { success }
}
"""

COMMENT_CREATE_MUTATION = """
mutation PostComment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id body createdAt user { id name displayName } }
  }
}
"""

COMMENTS_QUERY = """
query IssueComments($id: String!, $filter: CommentFilter, $first: Int!, $after: String) {
  issue(id: $id) {
    comments(filter: $filter, first: $first, after: $after) {
      nodes {
        id body createdAt
        user { id name displayName }
        botActor { name }
        externalUser { name displayName }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

CHECK_QUERY = """
query Check($labelFilter: IssueLabelFilter!, $teamFilter: TeamFilter, $projectFilter: ProjectFilter,
            $withTeam: Boolean!, $withProject: Boolean!) {
  viewer { id name }
  issueLabels(filter: $labelFilter, first: 1) { nodes { id } }
  teams(filter: $teamFilter, first: 1) @include(if: $withTeam) { nodes { id } }
  projects(filter: $projectFilter, first: 1) @include(if: $withProject) { nodes { id } }
}
"""


def _parse_time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _nodes(connection: Any) -> list[dict[str, Any]]:
    return list((connection or {}).get("nodes") or [])


class LinearAdapter(TrackerAdapter):
    def __init__(self, settings: Mapping[str, Any], *, transport: Transport | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._settings = dict(settings or {})
        self._key_env = str(self._settings.get("api_key_env") or DEFAULT_KEY_ENV)
        self._clock = clock
        self._client = JsonClient("linear", transport, clock=clock)
        self._viewer_id: str | None = None
        self._team_states: dict[str, list[dict[str, Any]]] = {}

    @property
    def name(self) -> str:
        return "linear"

    # -- transport ---------------------------------------------------------------------------

    def _graphql(self, query: str, variables: Mapping[str, Any] | None = None) -> dict[str, Any]:
        key = get_secret(self._key_env)
        if not key:
            raise AdapterError(f"linear: {self._key_env} is not set")
        resp = self._client.request("POST", API_URL, json_body={"query": query, "variables": dict(variables or {})},
                                    headers={"Authorization": key}, allow_statuses=(400,))
        payload = resp.data if isinstance(resp.data, dict) else {}
        errors = payload.get("errors") or []
        for error in errors:
            if ((error or {}).get("extensions") or {}).get("code") == "RATELIMITED":
                wait = retry_after(resp.headers, self._clock())
                raise RateLimited(wait, f"linear: rate limited; retry after {wait:g}s")
        if errors:
            message = (errors[0] or {}).get("message") or "unknown GraphQL error"
            raise AdapterError(f"linear: {message}")
        data = payload.get("data")
        if resp.status >= 300 or not isinstance(data, dict):
            detail = excerpt(json.dumps(resp.data).encode("utf-8")) if resp.data is not None else ""
            raise AdapterError(f"linear: HTTP {resp.status}" + (f": {detail}" if detail else ""))
        return data

    def _paginate(self, query: str, variables: Mapping[str, Any],
                  connection: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            page = connection(self._graphql(query, {**variables, "first": PAGE_SIZE, "after": after})) or {}
            nodes.extend(_nodes(page))
            info = page.get("pageInfo") or {}
            after = info.get("endCursor")
            if not info.get("hasNextPage") or not after:
                return nodes

    # -- lookups -----------------------------------------------------------------------------

    def _issue_ref(self, key: str) -> dict[str, Any]:
        issue = self._graphql(ISSUE_REF_QUERY, {"id": key}).get("issue")
        if not issue:
            raise AdapterError(f"linear: issue {key} not found")
        return issue

    def _states(self, team_id: str) -> list[dict[str, Any]]:
        states = self._team_states.get(team_id)
        if states is None:
            states = self._paginate(TEAM_STATES_QUERY, {"id": team_id},
                                    lambda data: (data.get("team") or {}).get("states"))
            # Workflow order, so the valid-names list and same-type ties follow the board.
            states.sort(key=lambda s: s["position"] if isinstance(s.get("position"), (int, float)) else 0.0)
            self._team_states[team_id] = states
        return states

    @staticmethod
    def _resolve_state(states: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
        wanted = target.strip().casefold()
        for state in states:
            if (state.get("name") or "").casefold() == wanted:
                return state
        typed = [s for s in states if (s.get("type") or "").casefold() == wanted]
        if not typed:
            return None
        if all(isinstance(s.get("position"), (int, float)) for s in typed):
            return min(typed, key=lambda s: s["position"])
        return typed[0]

    # -- TrackerAdapter ----------------------------------------------------------------------

    def fetch_issues(self, filter: Mapping[str, Any]) -> list[Issue]:
        filter = filter or {}
        issue_filter: dict[str, Any] = {
            "labels": {"some": {"name": {"eqIgnoreCase": str(filter.get("label") or DEFAULT_LABEL)}}}}
        if filter.get("project"):
            issue_filter["project"] = {"name": {"eq": str(filter["project"])}}
        if filter.get("team"):
            issue_filter["team"] = {"key": {"eq": str(filter["team"])}}
        nodes = self._paginate(ISSUES_QUERY, {"filter": issue_filter}, lambda data: data.get("issues"))
        return [
            Issue(
                key=node["identifier"],
                title=node.get("title") or "",
                body=node.get("description") or "",
                url=node.get("url") or "",
                state=(node.get("state") or {}).get("name") or "",
                labels=tuple(label["name"] for label in _nodes(node.get("labels")) if label.get("name")),
                updated_at=node.get("updatedAt"),
            )
            for node in nodes
        ]

    def move_issue(self, key: str, target_state: str) -> None:
        issue = self._issue_ref(key)
        current = issue.get("state") or {}
        if (current.get("name") or "").casefold() == target_state.strip().casefold():
            return
        team_id = (issue.get("team") or {}).get("id") or ""
        states = self._states(team_id)
        state = self._resolve_state(states, target_state)
        if state is None:
            names = ", ".join(s.get("name") or "?" for s in states) or "none"
            raise TransitionNotAllowed(f"linear: no state {target_state!r} for {key}; valid states: {names}")
        if state.get("id") == current.get("id"):
            return
        result = self._graphql(ISSUE_UPDATE_MUTATION, {"id": issue["id"], "stateId": state["id"]})
        if not (result.get("issueUpdate") or {}).get("success"):
            raise AdapterError(f"linear: moving {key} to {state.get('name')!r} was not accepted")

    def post_comment(self, key: str, body: str) -> Comment:
        issue = self._issue_ref(key)
        payload = self._graphql(COMMENT_CREATE_MUTATION, {"issueId": issue["id"], "body": body}).get("commentCreate")
        comment = (payload or {}).get("comment")
        if not (payload or {}).get("success") or not comment:
            raise AdapterError(f"linear: commenting on {key} was not accepted")
        user = comment.get("user") or {}
        return Comment(id=comment["id"], author=user.get("displayName") or user.get("name") or "",
                       body=comment.get("body") or body, created_at=comment.get("createdAt"), is_bot=True)

    def list_comments_since(self, key: str, cursor: str | None) -> tuple[list[Comment], str | None]:
        since = _parse_time(cursor) if cursor else None
        variables = {"id": key, "filter": {"createdAt": {"gt": cursor}} if cursor else None}

        def connection(data: dict[str, Any]) -> Any:
            issue = data.get("issue")
            if not issue:
                raise AdapterError(f"linear: issue {key} not found")
            return issue.get("comments")

        nodes = self._paginate(COMMENTS_QUERY, variables, connection)
        fresh = [(_parse_time(n["createdAt"]), n) for n in nodes if n.get("createdAt")]
        fresh = sorted((pair for pair in fresh if since is None or pair[0] > since), key=lambda pair: pair[0])
        if not fresh:
            return [], cursor
        comments = []
        for _, node in fresh:
            user = node.get("user") or {}
            other = node.get("botActor") or node.get("externalUser") or {}
            author = (user.get("displayName") or user.get("name") or other.get("displayName")
                      or other.get("name") or "unknown")
            comments.append(Comment(id=node["id"], author=author, body=node.get("body") or "",
                                    created_at=node["createdAt"],
                                    is_bot=is_signed(node.get("body"))))
        return comments, comments[-1].created_at

    def check(self) -> None:
        filter = self._settings.get("filter") or {}
        label = str(filter.get("label") or DEFAULT_LABEL)
        team = str(filter.get("team") or "")
        project = str(filter.get("project") or "")
        label_filter: dict[str, Any] = {"name": {"eqIgnoreCase": label}}
        if team:  # the team's own labels or workspace-wide ones
            label_filter["or"] = [{"team": {"key": {"eq": team}}}, {"team": {"null": True}}]
        data = self._graphql(CHECK_QUERY, {
            "labelFilter": label_filter,
            "teamFilter": {"key": {"eq": team}} if team else None,
            "projectFilter": {"name": {"eq": project}} if project else None,
            "withTeam": bool(team), "withProject": bool(project),
        })
        viewer_id = (data.get("viewer") or {}).get("id")
        if not viewer_id:
            raise AdapterError("linear: could not identify the API key's user; check your API key")
        self._viewer_id = viewer_id
        if team and not _nodes(data.get("teams")):
            raise AdapterError(f"linear: team {team!r} not found")
        if not _nodes(data.get("issueLabels")):
            raise AdapterError(f"linear: label {label!r} not found" + (f" in team {team!r}" if team else ""))
        if project and not _nodes(data.get("projects")):
            raise AdapterError(f"linear: project {project!r} not found")
