import json

import pytest

from issue_sync.signature import sign
from issue_sync.adapters.base import AdapterError, Issue, RateLimited, TransitionNotAllowed
from issue_sync.adapters.linear import API_URL, LinearAdapter

KEY = "lin_api_test_secret_value"
NOW = 1_794_000_000.0
TEAM_ID = "6f1c2a3e-0000-4000-8000-000000000001"
ISSUE_UUID = "0b7d9a52-1111-4111-8111-000000000002"
VIEWER_ID = "a9e3c1d0-2222-4222-8222-000000000003"
STATES = [
    {"id": "st-review", "name": "In Review", "type": "started", "position": 1002},
    {"id": "st-backlog", "name": "Backlog", "type": "backlog", "position": 0},
    {"id": "st-done", "name": "Done", "type": "completed", "position": 3},
    {"id": "st-todo", "name": "Todo", "type": "unstarted", "position": 1},
    {"id": "st-progress", "name": "In Progress", "type": "started", "position": 2},
    {"id": "st-canceled", "name": "Canceled", "type": "canceled", "position": 4},
]


class Transport:
    """Scripted Linear endpoint: each call pops the next ``(check, status, body, headers)`` step."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, method, url, headers, body):
        assert method == "POST" and url == API_URL
        assert headers["Authorization"] == KEY  # raw personal key, no "Bearer"
        sent = json.loads(body)
        self.requests.append(sent)
        assert self.steps, f"unexpected request: {sent['query'][:60]}"
        check, status, payload, resp_headers = self.steps.pop(0)
        check(sent["query"], sent["variables"])
        return status, resp_headers, json.dumps(payload).encode()

    def done(self):
        assert not self.steps, f"{len(self.steps)} scripted response(s) unused"


def step(operation, data=None, *, status=200, errors=None, headers=None, expect=None):
    """Expect a request whose query contains ``operation`` and (optionally) these variables."""
    def check(query, variables):
        assert operation in query, query
        for name, value in (expect or {}).items():
            assert variables.get(name) == value, (name, variables.get(name))
    payload = {"errors": errors} if errors else {"data": data}
    return check, status, payload, headers or {}


def page(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def issue_ref(state_name="Todo"):
    state = next(s for s in STATES if s["name"] == state_name)
    return step("IssueRef", {"issue": {"id": ISSUE_UUID, "state": {"id": state["id"], "name": state["name"]},
                                       "team": {"id": TEAM_ID}}}, expect={"id": "CORE-7"})


def team_states():
    return step("TeamStates", {"team": {"states": page(STATES)}}, expect={"id": TEAM_ID})


def move(state_id):
    return step("issueUpdate", {"issueUpdate": {"success": True}},
                expect={"id": ISSUE_UUID, "stateId": state_id})


def issue_node(n, state="Todo"):
    return {"identifier": f"CORE-{n}", "title": f"Issue {n}", "description": None if n % 2 else f"Body {n}",
            "url": f"https://linear.app/acme/issue/CORE-{n}/issue-{n}", "updatedAt": "2026-11-06T06:27:53.660Z",
            "state": {"name": state}, "labels": {"nodes": [{"name": "agent"}, {"name": "Bug"}]}}


def comment_node(cid, created, user_id="u-human", name="Ada", body=None):
    user = {"id": user_id, "name": name.lower(), "displayName": name} if user_id else None
    return {"id": cid, "body": body or f"comment {cid}", "createdAt": created, "user": user,
            "botActor": None if user_id else {"name": "GitHub"}, "externalUser": None}


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", KEY)


def adapter(transport, **settings):
    return LinearAdapter(settings, transport=transport, clock=lambda: NOW)


def test_fetch_issues_ands_filters_and_follows_pages(secret):
    expected_filter = {"labels": {"some": {"name": {"eqIgnoreCase": "agent"}}},
                       "project": {"name": {"eq": "Ticket Sync Sandbox"}}, "team": {"key": {"eq": "CORE"}}}
    t = Transport(
        step("issues(", {"issues": page([issue_node(1), issue_node(2, "In Progress")], "cur-1")},
             expect={"filter": expected_filter, "after": None}),
        step("issues(", {"issues": page([issue_node(3)])}, expect={"filter": expected_filter, "after": "cur-1"}),
    )
    issues = adapter(t).fetch_issues({"project": "Ticket Sync Sandbox", "team": "CORE"})
    t.done()
    assert [i.key for i in issues] == ["CORE-1", "CORE-2", "CORE-3"]
    assert issues[1] == Issue(key="CORE-2", title="Issue 2", body="Body 2",
                              url="https://linear.app/acme/issue/CORE-2/issue-2", state="In Progress",
                              labels=("agent", "Bug"), updated_at="2026-11-06T06:27:53.660Z")
    assert issues[0].body == ""  # null description


def test_fetch_issues_label_only_by_default(secret):
    t = Transport(step("issues(", {"issues": page([])},
                       expect={"filter": {"labels": {"some": {"name": {"eqIgnoreCase": "ready"}}}}}))
    assert adapter(t).fetch_issues({"label": "ready"}) == []
    t.done()


def test_move_matches_state_name_case_insensitively(secret):
    t = Transport(issue_ref("Todo"), team_states(), move("st-review"))
    adapter(t).move_issue("CORE-7", "in review")
    t.done()


def test_move_matches_state_type_picking_lowest_position(secret):
    # "started" is the type of both In Progress (2) and In Review (1002).
    t = Transport(issue_ref("Todo"), team_states(), move("st-progress"))
    adapter(t).move_issue("CORE-7", "Started")
    t.done()


def test_move_is_noop_when_already_in_state(secret):
    t = Transport(issue_ref("In Progress"))
    adapter(t).move_issue("CORE-7", "IN PROGRESS")
    t.done()
    t = Transport(issue_ref("In Progress"), team_states())  # matched by type to the current state
    adapter(t).move_issue("CORE-7", "started")
    t.done()


def test_unknown_state_lists_valid_names(secret):
    t = Transport(issue_ref("Todo"), team_states())
    with pytest.raises(TransitionNotAllowed) as exc:
        adapter(t).move_issue("CORE-7", "Shipped")
    t.done()
    assert str(exc.value) == ("linear: no state 'Shipped' for CORE-7; valid states: "
                              "Backlog, Todo, In Progress, Done, Canceled, In Review")


def test_team_states_are_cached(secret):
    t = Transport(issue_ref("Todo"), team_states(), move("st-progress"), issue_ref("In Progress"), move("st-done"))
    linear = adapter(t)
    linear.move_issue("CORE-7", "In Progress")
    linear.move_issue("CORE-7", "Done")
    t.done()
    assert sum("TeamStates" in r["query"] for r in t.requests) == 1


def test_failed_issue_update_raises(secret):
    t = Transport(issue_ref("Todo"), team_states(),
                  step("issueUpdate", {"issueUpdate": {"success": False}}))
    with pytest.raises(AdapterError, match="was not accepted"):
        adapter(t).move_issue("CORE-7", "Done")


def test_ratelimited_graphql_error_becomes_rate_limited(secret):
    reset_ms = str(int((NOW + 42) * 1000))
    t = Transport(step("issues(", status=400, headers={"X-RateLimit-Requests-Reset": reset_ms}, errors=[
        {"message": "Rate limit exceeded", "extensions": {"code": "RATELIMITED", "type": "ratelimited"}}]))
    with pytest.raises(RateLimited) as exc:
        adapter(t).fetch_issues({})
    assert exc.value.retry_after == pytest.approx(42)


def test_ratelimited_with_http_200_is_still_rate_limited(secret):
    t = Transport(step("IssueRef", errors=[{"message": "slow down", "extensions": {"code": "RATELIMITED"}}]))
    with pytest.raises(RateLimited) as exc:
        adapter(t).move_issue("CORE-7", "Done")
    assert exc.value.retry_after == 60  # no reset header -> default


def test_graphql_error_message_is_surfaced(secret):
    t = Transport(step("IssueRef", errors=[{"message": "Entity not found: Issue", "path": ["issue"],
                                             "extensions": {"code": "INPUT_ERROR", "statusCode": 400}}]))
    with pytest.raises(AdapterError) as exc:
        adapter(t).move_issue("CORE-7", "Done")
    assert str(exc.value) == "linear: Entity not found: Issue"


def test_http_400_without_graphql_errors_reports_status(secret):
    t = Transport((lambda q, v: None, 400, {"oops": "bad gateway thing"}, {}))
    with pytest.raises(AdapterError) as exc:
        adapter(t).fetch_issues({})
    assert str(exc.value).startswith("linear: HTTP 400: ") and "bad gateway thing" in str(exc.value)


def test_http_401_message(secret):
    t = Transport(step("Check", status=401, errors=[
        {"message": "Authentication required, not authenticated", "extensions": {"code": "AUTHENTICATION_ERROR"}}]))
    with pytest.raises(AdapterError) as exc:
        adapter(t).check()
    assert str(exc.value) == "linear: HTTP 401: not authorized; check your API key/token"
    assert KEY not in str(exc.value)


def test_missing_key_names_the_env_var(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    monkeypatch.delenv("MY_LINEAR", raising=False)
    t = Transport()
    with pytest.raises(AdapterError, match=r"^linear: LINEAR_API_KEY is not set$"):
        adapter(t).fetch_issues({})
    with pytest.raises(AdapterError, match=r"^linear: MY_LINEAR is not set$"):
        adapter(t, api_key_env="MY_LINEAR").check()
    assert t.requests == []


def test_custom_key_env_is_read_at_request_time(monkeypatch):
    monkeypatch.delenv("MY_LINEAR", raising=False)
    t = Transport(step("issues(", {"issues": page([])}))
    linear = adapter(t, api_key_env="MY_LINEAR")  # constructed before the secret exists
    monkeypatch.setenv("MY_LINEAR", KEY)
    assert linear.fetch_issues({}) == []


def test_comments_strictly_newer_oldest_first_across_pages(secret):
    cursor = "2026-11-02T02:56:21.572Z"
    t = Transport(
        step("IssueComments", {"issue": {"comments": page([
            comment_node("c3", "2026-11-02T03:30:00.000Z", VIEWER_ID, "Hermes", sign("done")),
            comment_node("c0", "2026-11-02T02:56:21.572Z"),  # equal to cursor: excluded
        ], "p2")}}, expect={"id": "CORE-7", "filter": {"createdAt": {"gt": cursor}}, "after": None}),
        step("IssueComments", {"issue": {"comments": page([
            comment_node("c2", "2026-11-02T04:00:01+01:00", None),  # 03:00:01Z, offset format
            comment_node("old", "2026-11-01T09:00:00.000Z"),  # older than cursor: excluded
        ])}}, expect={"after": "p2"}),
        step("IssueComments", {"issue": {"comments": page([comment_node("c4", "2026-11-02T05:00:00.000Z")])}}),
    )
    linear = adapter(t)
    comments, new_cursor = linear.list_comments_since("CORE-7", cursor)
    assert [c.id for c in comments] == ["c2", "c3"]
    assert new_cursor == "2026-11-02T03:30:00.000Z"
    assert [(c.author, c.is_bot) for c in comments] == [("GitHub", False), ("Hermes", True)]
    more, newer = linear.list_comments_since("CORE-7", new_cursor)
    t.done()
    assert [(c.id, c.author, c.is_bot) for c in more] == [("c4", "Ada", False)]
    assert newer == "2026-11-02T05:00:00.000Z"


def test_comments_none_new_keeps_cursor(secret):
    t = Transport(step("IssueComments", {"issue": {"comments": page([])}}, expect={"filter": None}),
                  step("IssueComments", {"issue": {"comments": page([])}}))
    assert adapter(t).list_comments_since("CORE-7", None) == ([], None)
    assert adapter(t).list_comments_since("CORE-7", "2026-11-02T03:00:00Z") == ([], "2026-11-02T03:00:00Z")
    t.done()


def test_post_comment_resolves_issue_uuid(secret):
    t = Transport(issue_ref(), step("commentCreate", {"commentCreate": {"success": True, "comment": {
        "id": "cm-1", "body": "hello", "createdAt": "2026-11-07T10:00:00.000Z",
        "user": {"id": VIEWER_ID, "name": "hermes", "displayName": "Hermes"}}}},
        expect={"issueId": ISSUE_UUID, "body": "hello"}))
    comment = adapter(t).post_comment("CORE-7", "hello")
    t.done()
    assert (comment.id, comment.author, comment.body, comment.created_at, comment.is_bot) == (
        "cm-1", "Hermes", "hello", "2026-11-07T10:00:00.000Z", True)


def check_data(labels=1, projects=1, teams=None):
    data = {"viewer": {"id": VIEWER_ID, "name": "Hermes"}, "issueLabels": page([{"id": "l"}] * labels)}
    if projects is not None:
        data["projects"] = page([{"id": "p"}] * projects)
    if teams is not None:
        data["teams"] = page([{"id": TEAM_ID}] * teams)
    return data


def test_check_success_caches_viewer(secret):
    t = Transport(
        step("Check", check_data(teams=1), expect={
            "labelFilter": {"name": {"eqIgnoreCase": "agent"},
                            "or": [{"team": {"key": {"eq": "CORE"}}}, {"team": {"null": True}}]},
            "projectFilter": {"name": {"eq": "Sandbox"}}, "withProject": True,
            "teamFilter": {"key": {"eq": "CORE"}}, "withTeam": True}),
        step("IssueComments", {"issue": {"comments": page([comment_node("c1", "2026-11-02T03:00:00Z", VIEWER_ID, body=sign("x"))])}}),
    )
    linear = adapter(t, filter={"project": "Sandbox", "team": "CORE"})
    linear.check()
    comments, _ = linear.list_comments_since("CORE-7", None)
    t.done()  # no separate Viewer query
    assert comments[0].is_bot


def test_check_label_missing(secret):
    t = Transport(step("Check", check_data(labels=0, projects=None),
                       expect={"labelFilter": {"name": {"eqIgnoreCase": "ready"}}, "withProject": False}))
    with pytest.raises(AdapterError) as exc:
        adapter(t, filter={"label": "ready"}).check()
    assert str(exc.value) == "linear: label 'ready' not found"


def test_check_label_defaults_to_agent(secret):
    t = Transport(step("Check", check_data(labels=0, projects=None)))
    with pytest.raises(AdapterError, match=r"^linear: label 'agent' not found$"):
        adapter(t).check()


def test_check_project_missing(secret):
    t = Transport(step("Check", check_data(projects=0)))
    with pytest.raises(AdapterError, match=r"^linear: project 'Sandbox' not found$"):
        adapter(t, filter={"project": "Sandbox"}).check()


def test_name():
    assert LinearAdapter({}).name == "linear"
