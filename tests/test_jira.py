import base64
import json
import urllib.parse

import pytest

from ticket_sync.signature import sign
from ticket_sync.adapters import get_adapter
from ticket_sync.adapters.base import AdapterError, Issue, RateLimited, TransitionNotAllowed
from ticket_sync.adapters.jira import JiraAdapter, build_jql

BASE = "https://acme.atlassian.net"
EMAIL = "bot@example.com"
TOKEN = "jira_test_token_value"
AUTH = "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
ME = "acct-bot"
NOW = 1_794_000_000.0


class Transport:
    """Scripted Jira endpoint: each call pops ``(method, path, status, payload, headers, check)``."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, method, url, headers, body):
        assert headers["Authorization"] == AUTH
        parsed = urllib.parse.urlsplit(url)
        assert f"{parsed.scheme}://{parsed.netloc}" == BASE
        sent = json.loads(body) if body else None
        query = dict(urllib.parse.parse_qsl(parsed.query))
        self.requests.append((method, parsed.path, query, sent))
        assert self.steps, f"unexpected request: {method} {parsed.path}"
        want_method, want_path, status, payload, resp_headers, check = self.steps.pop(0)
        assert (method, parsed.path) == (want_method, want_path), (method, parsed.path)
        if check:
            check(query, sent)
        raw = b"" if payload is None else json.dumps(payload).encode()
        return status, resp_headers or {}, raw

    def done(self):
        assert not self.steps, f"{len(self.steps)} scripted response(s) unused"


def step(method, path, payload=None, *, status=200, headers=None, check=None):
    return method, path, status, payload, headers, check


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", BASE + "/")
    monkeypatch.setenv("JIRA_EMAIL", EMAIL)
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)


def adapter(transport, **settings):
    return JiraAdapter(settings, transport=transport, clock=lambda: NOW)


def issue_node(n, status="Backlog", description=None):
    return {"key": f"TSS-{n}", "fields": {
        "summary": f"Issue {n}", "description": description, "status": {"name": status},
        "labels": ["agent"], "updated": "2026-11-07T05:41:08.540+0000"}}


def comment_node(cid, created, account="acct-human", name="Ada", text="hello"):
    return {"id": cid, "created": created, "author": {"accountId": account, "displayName": name},
            "body": {"type": "doc", "version": 1,
                     "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}}


# -- JQL ----------------------------------------------------------------------------------------

def test_build_jql_project_and_label_with_quoting():
    assert build_jql({"project": "TSS", "label": "agent"}) == 'project = "TSS" AND labels = "agent" ORDER BY created ASC'
    assert build_jql({"project": 'we"ird'}) == 'project = "we\\"ird" AND labels = "agent" ORDER BY created ASC'
    assert build_jql({}) == 'labels = "agent" ORDER BY created ASC'


def test_raw_jql_wins():
    assert build_jql({"jql": "assignee = currentUser()", "project": "TSS"}) == "assignee = currentUser()"


# -- fetch --------------------------------------------------------------------------------------

def test_fetch_issues_uses_search_jql_and_follows_page_tokens(secrets):
    description = {"type": "doc", "version": 1,
                   "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Do the thing"}]}]}

    def first(query, body):
        assert body["jql"] == 'project = "TSS" AND labels = "agent" ORDER BY created ASC'
        assert "nextPageToken" not in body and "summary" in body["fields"]

    def second(query, body):
        assert body["nextPageToken"] == "tok-2"

    t = Transport(
        step("POST", "/rest/api/3/search/jql",
             {"issues": [issue_node(1, description=description), issue_node(2, "In Progress")],
              "nextPageToken": "tok-2", "isLast": False}, check=first),
        step("POST", "/rest/api/3/search/jql", {"issues": [issue_node(3)], "isLast": True}, check=second),
    )
    issues = adapter(t).fetch_issues({"project": "TSS", "label": "agent"})
    t.done()
    assert [i.key for i in issues] == ["TSS-1", "TSS-2", "TSS-3"]
    assert issues[0] == Issue(key="TSS-1", title="Issue 1", body="Do the thing", url=f"{BASE}/browse/TSS-1",
                              state="Backlog", labels=("agent",), updated_at="2026-11-07T05:41:08.540+0000")
    assert issues[1].state == "In Progress" and issues[1].body == ""


# -- move ---------------------------------------------------------------------------------------

TRANSITIONS = {"transitions": [
    {"id": "11", "name": "Start work", "to": {"name": "In Progress"}},
    {"id": "21", "name": "Finish", "to": {"name": "Done"}},
    {"id": "31", "name": "Selected for Development", "to": {"name": "Selected for Development"}},
]}


def status_of(name):
    return step("GET", "/rest/api/3/issue/TSS-1", {"fields": {"status": {"name": name}}},
                check=lambda q, b: q.get("fields") == "status")


def test_move_runs_transition_matching_target_status(secrets):
    t = Transport(
        status_of("Backlog"),
        step("GET", "/rest/api/3/issue/TSS-1/transitions", TRANSITIONS),
        step("POST", "/rest/api/3/issue/TSS-1/transitions", None, status=204,
             check=lambda q, b: b == {"transition": {"id": "11"}}),
    )
    adapter(t).move_issue("TSS-1", "in progress")
    t.done()


def test_move_falls_back_to_transition_name(secrets):
    t = Transport(
        status_of("Backlog"),
        step("GET", "/rest/api/3/issue/TSS-1/transitions", TRANSITIONS),
        step("POST", "/rest/api/3/issue/TSS-1/transitions", None, status=204,
             check=lambda q, b: b == {"transition": {"id": "21"}}),
    )
    adapter(t).move_issue("TSS-1", "Finish")
    t.done()


def test_move_is_noop_when_already_in_status(secrets):
    t = Transport(status_of("Done"))
    adapter(t).move_issue("TSS-1", "DONE")
    t.done()


def test_move_without_matching_transition_names_reachable_statuses(secrets):
    t = Transport(status_of("Backlog"), step("GET", "/rest/api/3/issue/TSS-1/transitions", TRANSITIONS))
    with pytest.raises(TransitionNotAllowed) as exc:
        adapter(t).move_issue("TSS-1", "In Review")
    assert "Done, In Progress, Selected for Development" in str(exc.value)
    t.done()


def test_move_missing_issue(secrets):
    t = Transport(step("GET", "/rest/api/3/issue/TSS-9", {"errorMessages": ["Issue does not exist"]}, status=404))
    with pytest.raises(AdapterError, match="TSS-9 not found"):
        adapter(t).move_issue("TSS-9", "Done")


# -- comments -----------------------------------------------------------------------------------

def test_post_comment_sends_adf(secrets):
    def check(query, body):
        texts = [p["content"][0]["text"] for p in body["body"]["content"]]
        assert texts == ["Done by Hermes", "PR: https://example.com/pr/1"]

    t = Transport(
        step("POST", "/rest/api/3/issue/TSS-1/comment",
             comment_node("c1", "2026-11-07T06:00:00.000+0000", account=ME, name="Bot", text="Done by Hermes"),
             status=201, check=check),
    )
    comment = adapter(t).post_comment("TSS-1", "Done by Hermes\nPR: https://example.com/pr/1")
    t.done()
    assert comment.id == "c1" and comment.is_bot


def test_list_comments_since_filters_orders_and_flags_bot(secrets):
    nodes = [
        comment_node("c3", "2026-11-07T07:00:00.000+0000", text="newest"),
        comment_node("c1", "2026-11-07T05:00:00.000+0000", text="old"),
        comment_node("c2", "2026-11-07T06:00:00.000+0000", account="acct-human", name="Ada", text=sign("mine")),
    ]
    t = Transport(
        step("GET", "/rest/api/3/issue/TSS-1/comment", {"comments": nodes, "total": 3},
             check=lambda q, b: q["orderBy"] == "created" and q["startAt"] == "0"),
    )
    comments, cursor = adapter(t).list_comments_since("TSS-1", "2026-11-07T05:30:00.000+0000")
    t.done()
    assert [c.id for c in comments] == ["c2", "c3"]
    assert [c.is_bot for c in comments] == [True, False]
    assert comments[1].body == "newest" and comments[1].author == "Ada"
    assert comments[0].author == "Ada"  # same account as a human: told apart by the signature
    assert cursor == "2026-11-07T07:00:00.000+0000"


def test_list_comments_paginates_and_keeps_cursor_when_nothing_new(secrets):
    t = Transport(
        step("GET", "/rest/api/3/issue/TSS-1/comment",
             {"comments": [comment_node("c1", "2026-11-07T05:00:00.000+0000")], "total": 2}),
        step("GET", "/rest/api/3/issue/TSS-1/comment",
             {"comments": [comment_node("c2", "2026-11-07T06:00:00.000+0000")], "total": 2},
             check=lambda q, b: q["startAt"] == "1"),
    )
    comments, cursor = adapter(t).list_comments_since("TSS-1", "2026-11-07T06:00:00Z")
    t.done()
    assert comments == [] and cursor == "2026-11-07T06:00:00Z"


# -- errors, auth, check -------------------------------------------------------------------------

def test_rate_limit_uses_retry_after(secrets):
    t = Transport(step("POST", "/rest/api/3/search/jql", {"errorMessages": ["slow down"]}, status=429,
                       headers={"Retry-After": "17"}))
    with pytest.raises(RateLimited) as exc:
        adapter(t).fetch_issues({"project": "TSS"})
    assert exc.value.retry_after == 17


def test_401_message_points_at_token_and_hides_it(secrets):
    t = Transport(step("GET", "/rest/api/3/myself", {"errorMessages": ["nope"]}, status=401))
    with pytest.raises(AdapterError) as exc:
        adapter(t).check()
    assert "check your API key/token" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_missing_credentials_name_the_variables(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    with pytest.raises(AdapterError, match="JIRA_EMAIL, JIRA_API_TOKEN not set"):
        adapter(Transport()).check()


def test_base_url_must_be_https(monkeypatch, secrets):
    monkeypatch.setenv("JIRA_BASE_URL", "http://acme.atlassian.net")
    with pytest.raises(AdapterError, match="https://"):
        adapter(Transport()).check()


def test_check_verifies_project(secrets):
    t = Transport(step("GET", "/rest/api/3/myself", {"accountId": ME}),
                  step("GET", "/rest/api/3/project/NOPE", {"errorMessages": ["missing"]}, status=404))
    with pytest.raises(AdapterError, match="project 'NOPE' not found"):
        adapter(t, filter={"project": "NOPE"}).check()
    t.done()
    t = Transport(step("GET", "/rest/api/3/myself", {"accountId": ME}),
                  step("GET", "/rest/api/3/project/TSS", {"key": "TSS"}))
    adapter(t, filter={"project": "TSS"}).check()
    t.done()


def test_custom_env_names(monkeypatch):
    monkeypatch.setenv("MY_JIRA_URL", BASE)
    monkeypatch.setenv("MY_JIRA_MAIL", EMAIL)
    monkeypatch.setenv("MY_JIRA_TOKEN", TOKEN)
    t = Transport(step("GET", "/rest/api/3/myself", {"accountId": ME}))
    adapter(t, base_url_env="MY_JIRA_URL", email_env="MY_JIRA_MAIL", token_env="MY_JIRA_TOKEN").check()
    t.done()


def test_registry_builds_jira_adapter():
    assert get_adapter("jira", {}).name == "jira"
