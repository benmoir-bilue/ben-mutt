from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bem.ai.tools import AGENT_TOOLS, ToolExecutor
from bem.gmail.models import Label, Thread


class FakeGmail:
    def __init__(self, threads=None, labels=None):
        self.threads = threads or []
        self.labels = labels or []

    def list_labels(self):
        return self.labels

    def list_threads(self, label_id="", max_results=25, page_token=None, query=""):
        return self.threads[:max_results], None

    def get_thread(self, thread_id):
        return next((t for t in self.threads if t.id == thread_id), None)


@pytest.fixture
def threads(make_message):
    return [
        Thread(id=f"t{i}", snippet=f"snip {i}",
               messages=[make_message(id=f"m{i}", thread_id=f"t{i}",
                                      subject=f"Subject {i}")])
        for i in range(1, 4)
    ]


@pytest.fixture
def executor(threads):
    gmail = FakeGmail(
        threads=threads,
        labels=[Label(id="L1", name="Finance", type="user", messages_total=10),
                Label(id="INBOX", name="INBOX", type="system", messages_total=50)],
    )
    return ToolExecutor(gmail, threads)


class TestSchemas:
    def test_every_tool_has_a_handler(self, executor):
        for tool in AGENT_TOOLS:
            assert hasattr(executor, f"_tool_{tool['name']}"), tool["name"]

    def test_schemas_are_closed(self):
        for tool in AGENT_TOOLS:
            assert tool["input_schema"]["additionalProperties"] is False


class TestReadOnly:
    def test_list_labels(self, executor):
        out, err = executor.execute("list_labels", {})
        assert not err
        assert "Finance" in out and "user" in out

    def test_search_registers_threads(self, threads):
        gmail = FakeGmail(threads=threads)
        ex = ToolExecutor(gmail)  # starts knowing nothing
        out, err = ex.execute("search_threads", {"query": "in:inbox"})
        assert not err
        assert "t1 |" in out
        # now the agent may file what it found
        _, err = ex.execute("file_thread", {"thread_id": "t1",
                                            "label_name": "Finance", "reason": "x"})
        assert not err

    def test_get_thread_includes_body(self, executor):
        out, err = executor.execute("get_thread", {"thread_id": "t1"})
        assert not err
        assert "Subject 1" in out

    def test_get_thread_missing(self, executor):
        out, err = executor.execute("get_thread", {"thread_id": "nope"})
        assert err


class TestDeferredMutations:
    def test_file_thread_queues_not_executes(self, executor):
        out, err = executor.execute(
            "file_thread",
            {"thread_id": "t1", "label_name": "Finance", "reason": "invoice"})
        assert not err and "queued" in out
        assert len(executor.plan) == 1
        action = executor.plan[0]
        assert (action.kind, action.label_name, action.subject) == \
            ("file", "Finance", "Subject 1")

    def test_refiling_replaces_earlier_action(self, executor):
        executor.execute("file_thread", {"thread_id": "t1",
                                         "label_name": "Finance", "reason": "a"})
        executor.execute("archive_thread", {"thread_id": "t1", "reason": "b"})
        assert len(executor.plan) == 1
        assert executor.plan[0].kind == "archive"

    def test_unknown_thread_rejected(self, executor):
        out, err = executor.execute(
            "file_thread", {"thread_id": "tx", "label_name": "Finance", "reason": "x"})
        assert err
        assert executor.plan == []

    def test_empty_label_rejected(self, executor):
        out, err = executor.execute(
            "file_thread", {"thread_id": "t1", "label_name": "  ", "reason": "x"})
        assert err

    def test_unknown_tool(self, executor):
        out, err = executor.execute("rm_rf", {})
        assert err

    def test_bad_args(self, executor):
        out, err = executor.execute("file_thread", {"thread_id": "t1"})
        assert err
