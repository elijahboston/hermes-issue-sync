import json
import subprocess

import pytest

from issue_sync.kanban import FakeKanban, KanbanClient, KanbanError


class Runner:
    """Stands in for ``subprocess.run``: records argv/kwargs, returns canned output."""

    def __init__(self, stdout="", *, returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def argv(self):
        return self.calls[-1][0]

    @property
    def kwargs(self):
        return self.calls[-1][1]


CARD = {"id": "t_0000abcd", "title": "[FAKE-1] Fix", "body": "", "assignee": None, "status": "ready"}


# ── argv construction ─────────────────────────────────────────────────────

def test_board_goes_before_subcommand():
    runner = Runner("[]")
    KanbanClient("ops", runner=runner).list_cards()
    assert runner.argv == ["hermes", "kanban", "--board", "ops", "list", "--json"]


def test_no_board_flag_without_board_and_runner_kwargs():
    runner = Runner("[]")
    KanbanClient(hermes_bin="/opt/hermes", timeout=7.5, runner=runner).list_cards()
    assert runner.argv == ["/opt/hermes", "kanban", "list", "--json"]
    assert runner.kwargs == {"input": None, "capture_output": True, "text": True, "timeout": 7.5, "check": False}


def test_list_status_and_archived_flags_and_parsing():
    runner = Runner(json.dumps([CARD, "junk"]))
    cards = KanbanClient(runner=runner).list_cards("done", archived=True)
    assert runner.argv == ["hermes", "kanban", "list", "--json", "--status", "done", "--archived"]
    assert cards == [CARD]


def test_list_rejects_non_array():
    with pytest.raises(KanbanError, match="expected a JSON array"):
        KanbanClient(runner=Runner('{"tasks": []}')).list_cards()


def test_show_parses_full_payload():
    payload = {"task": CARD, "latest_summary": "Shipped", "parents": [], "children": [],
               "comments": [{"author": "me", "body": "hi", "created_at": 1}], "events": [], "runs": []}
    runner = Runner(json.dumps(payload))
    assert KanbanClient("ops", runner=runner).show_card("t_0000abcd") == payload
    assert runner.argv == ["hermes", "kanban", "--board", "ops", "show", "t_0000abcd", "--json"]


def test_show_without_task_is_an_error():
    with pytest.raises(KanbanError, match="no task"):
        KanbanClient(runner=Runner('{"error": "nope"}')).show_card("t_1")


def test_create_passes_body_on_stdin():
    runner = Runner('{"id": "t_0000beef"}')
    card_id = KanbanClient("ops", runner=runner).create_card(
        "[FAKE-1] Fix", "<!-- marker -->\n\nbody\n", idempotency_key="issue-sync:fake:FAKE-1", assignee="worker")
    assert card_id == "t_0000beef"
    assert runner.argv == ["hermes", "kanban", "--board", "ops", "create", "[FAKE-1] Fix", "--body-file", "-",
                           "--idempotency-key", "issue-sync:fake:FAKE-1", "--assignee", "worker", "--json"]
    assert runner.kwargs["input"] == "<!-- marker -->\n\nbody\n"


def test_create_without_assignee_omits_flag():
    runner = Runner('{"id": "t_1"}')
    KanbanClient(runner=runner).create_card("T", "B", idempotency_key="k")
    assert "--assignee" not in runner.argv


@pytest.mark.parametrize("stdout", [
    '{"id": "t_0000beef", "title": "x"}',
    '{"task": {"id": "t_0000beef", "status": "ready"}}',
    '"t_0000beef"',
    "t_0000beef\n",
    "Created task:\nt_0000beef  [FAKE-1] Fix\n",
])
def test_create_parses_id_tolerantly(stdout):
    assert KanbanClient(runner=Runner(stdout)).create_card("T", "B", idempotency_key="k") == "t_0000beef"


@pytest.mark.parametrize("stdout", ["", "{}", "Created a task", '{"task": {}}'])
def test_create_without_id_is_an_error(stdout):
    with pytest.raises(KanbanError, match="no card id"):
        KanbanClient(runner=Runner(stdout)).create_card("T", "B", idempotency_key="k")


def test_comment_uses_author_and_end_of_options():
    runner = Runner("")
    KanbanClient("ops", runner=runner).comment("t_1", "--not a flag")
    assert runner.argv == ["hermes", "kanban", "--board", "ops", "comment", "--author", "issue-sync", "--",
                           "t_1", "--not a flag"]


def test_unblock_argv():
    runner = Runner("")
    KanbanClient(runner=runner).unblock("t_1", "answered on tracker")
    assert runner.argv == ["hermes", "kanban", "unblock", "t_1", "--reason", "answered on tracker"]


# ── failures ──────────────────────────────────────────────────────────────

def test_nonzero_exit_raises_with_trimmed_stderr():
    runner = Runner(returncode=2, stderr="error: no such board\n" + "x" * 1000)
    with pytest.raises(KanbanError) as info:
        KanbanClient("missing", runner=runner).list_cards()
    message = str(info.value)
    assert "kanban list exited with status 2" in message
    assert "no such board" in message
    assert len(message) < 400


def test_missing_binary_raises():
    with pytest.raises(KanbanError, match="not found"):
        KanbanClient(runner=Runner(exc=FileNotFoundError("hermes"))).comment("t_1", "hi")


def test_timeout_raises():
    exc = subprocess.TimeoutExpired(["hermes"], 30)
    with pytest.raises(KanbanError, match="timed out"):
        KanbanClient(runner=Runner(exc=exc)).show_card("t_1")


def test_bad_json_raises():
    with pytest.raises(KanbanError, match="invalid JSON"):
        KanbanClient(runner=Runner("not json")).list_cards()


# ── FakeKanban ────────────────────────────────────────────────────────────

def test_fake_create_is_idempotent_unless_archived():
    kanban = FakeKanban()
    first = kanban.create_card("T", "B", idempotency_key="issue-sync:fake:A")
    assert kanban.create_card("T2", "B2", idempotency_key="issue-sync:fake:A") == first
    assert kanban.cards[first]["status"] == "ready"
    assert len(kanban.cards) == 1
    other = kanban.create_card("T", "B", idempotency_key="issue-sync:fake:B")
    assert other != first
    kanban.set_status(first, "archived")
    recreated = kanban.create_card("T", "B", idempotency_key="issue-sync:fake:A")
    assert recreated not in (first, other)


def test_fake_list_show_comment():
    kanban = FakeKanban()
    live = kanban.add_card("Live", status="running", summary="did it")
    gone = kanban.add_card("Gone", status="archived")
    assert [c["id"] for c in kanban.list_cards()] == [live]
    assert {c["id"] for c in kanban.list_cards(archived=True)} == {live, gone}
    assert kanban.list_cards("archived", archived=True)[0]["id"] == gone

    kanban.comment(live, "hello")
    show = kanban.show_card(live)
    assert show["task"]["status"] == "running"
    assert show["latest_summary"] == "did it"
    assert [(c["author"], c["body"]) for c in show["comments"]] == [("issue-sync", "hello")]
    assert isinstance(show["comments"][0]["created_at"], int)

    with pytest.raises(KanbanError):
        kanban.show_card("t_unknown")
    kanban.fail_next("show_card", KanbanError("down"))
    with pytest.raises(KanbanError, match="down"):
        kanban.show_card(live)
    assert kanban.show_card(live)["task"]["id"] == live
