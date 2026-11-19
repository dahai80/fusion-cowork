"""issue #546: support mailbox agent — IMAP poller + classify pipeline, P0 observe-only.

Acceptance: real-ish inbound mail classified into feedback rows; duplicate
message-id produces no duplicate rows; poller crash recovers; P0 = zero
outbound except digest; low-confidence/sensitive = human queue (fail-open
to human, never to automation).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from fusion_cowork.mailbox.classifier import MailClassifier, parse_classification_json
from fusion_cowork.mailbox.digest import DigestBuilder
from fusion_cowork.mailbox.poller import MailboxPoller, imap_config_from_env
from fusion_cowork.mailbox.service import MailboxAgent
from fusion_cowork.mailbox.store import FeedbackStore


class _FakeImap:
    # minimal imaplib-shaped fake; per-test fetch results injected
    def __init__(self, results: dict):
        self._results = results
        self.logged_in = False

    def login(self, user, password):
        self.logged_in = True

    def select(self, folder, readonly=True):
        return "OK", [b"1"]

    def logout(self):
        pass

    def uid(self, cmd, *args):
        if cmd == "search":
            uids = self._results.get("uids", b"1 2")
            return "OK", [uids]
        fetches = self._results.get("fetches", {})
        uid = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
        raw = fetches.get(uid)
        if raw is None:
            return "OK", [None]
        return "OK", [(b"1 (RFC822 {1}", raw)]


def _rfc822(message_id: str, from_addr: str, subject: str, body: str = "hello") -> bytes:
    return (
        f"From: {from_addr}\r\nTo: support@fusion-mlx.cn\r\nSubject: {subject}\r\n"
        f"Date: Wed, 19 Nov 2026 08:00:00 +0000\r\nMessage-ID: <{message_id}>\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n"
    ).encode()


class _FakeMlx:
    def __init__(self, reply: str = "", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls = 0

    async def chat(self, model, messages, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("mlx down")
        return type("R", (), {"content": self.reply})()


_GOOD_REPLY = (
    '{"category": "bug", "sentiment": "neutral", "urgency": "medium", "confidence": 0.85, "reason": "crash report"}'
)


@pytest.fixture
def store(tmp_path):
    return FeedbackStore(db_path=str(tmp_path / "fb.db"))


class TestPoller:
    def test_imap_config_from_env_incomplete(self, monkeypatch):
        monkeypatch.delenv("FUSION_MAILBOX_IMAP_HOST", raising=False)
        monkeypatch.delenv("FUSION_MAILBOX_IMAP_USER", raising=False)
        monkeypatch.delenv("FUSION_MAILBOX_IMAP_PASS", raising=False)
        assert imap_config_from_env() is None
        monkeypatch.setenv("FUSION_MAILBOX_IMAP_HOST", "imap.example.com")
        monkeypatch.setenv("FUSION_MAILBOX_IMAP_USER", "support@fusion-mlx.cn")
        monkeypatch.setenv("FUSION_MAILBOX_IMAP_PASS", "secret")
        cfg = imap_config_from_env()
        assert cfg["host"] == "imap.example.com" and cfg["port"] == 993 and cfg["folder"] == "INBOX"

    async def test_fetch_new_parses_messages(self):
        fake = _FakeImap(
            {
                "uids": b"5 6",
                "fetches": {
                    "5": _rfc822("a@x", "user@x.com", "Crash on start", "it crashes"),
                    "6": _rfc822("b@x", "other@x.com", "Feature ask", "please add"),
                },
            }
        )
        p = MailboxPoller(
            {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX"}, imap_factory=lambda cfg: fake
        )
        result = await p.fetch_new("")
        assert len(result["messages"]) == 2
        assert result["messages"][0]["message_id"] == "<a@x>"
        assert result["messages"][0]["from_addr"] == "user@x.com"
        assert result["last_uid"] == "6"

    async def test_fetch_incremental_skips_known_uid(self):
        fake = _FakeImap({"uids": b"5 6", "fetches": {"6": _rfc822("b@x", "o@x", "S", "b")}})
        p = MailboxPoller(
            {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX"}, imap_factory=lambda cfg: fake
        )
        result = await p.fetch_new("5")
        assert [m["message_id"] for m in result["messages"]] == ["<b@x>"]

    async def test_fetch_error_raises_for_retry(self):
        class _Boom:
            def login(self, *a):
                raise RuntimeError("imap down")

            def logout(self):
                pass

        p = MailboxPoller(
            {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX"},
            imap_factory=lambda cfg: _Boom(),
        )
        with pytest.raises(RuntimeError):
            await p.fetch_new("")


class TestClassifier:
    def test_rule_prefilter_noreply_and_bounce(self):
        c = MailClassifier(mlx=_FakeMlx())
        r1 = asyncio.run(c.classify({"from_addr": "no-reply@x.com", "subject": "hi", "body": ""}))
        assert r1["category"] == "spam" and r1["confidence"] >= 0.9 and not r1["human_queue"]
        r2 = asyncio.run(c.classify({"from_addr": "a@x.com", "subject": "Undelivered Mail Returned", "body": ""}))
        assert r2["category"] == "spam" and r2["rule_prefilter"]

    def test_parse_classification_json_variants(self):
        ok = parse_classification_json('```json\n{"category": "bug", "confidence": 0.9}\n```')
        assert ok["category"] == "bug" and ok["sentiment"] == "neutral" and ok["urgency"] == "medium"
        assert parse_classification_json('junk {"category": "nope"} more') is None
        assert parse_classification_json("not json") is None
        assert parse_classification_json('{"category": "bug", "confidence": "abc"}')["confidence"] == 0.0

    async def test_llm_classify_good(self):
        c = MailClassifier(mlx=_FakeMlx(reply=_GOOD_REPLY))
        r = await c.classify({"from_addr": "u@x.com", "subject": "bug", "body": "crash"})
        assert r["category"] == "bug" and r["routed_to"] == "mined_issues+auto_ack"
        assert not r["human_queue"]

    async def test_low_confidence_goes_human_queue(self):
        c = MailClassifier(mlx=_FakeMlx(reply='{"category": "bug", "confidence": 0.3}'))
        r = await c.classify({"from_addr": "u@x.com", "subject": "s", "body": "b"})
        assert r["human_queue"] and r["routed_to"] == "human_queue"

    async def test_sensitive_classes_always_human(self):
        for reply in (
            '{"category": "business", "confidence": 0.99}',
            '{"category": "data_rights", "confidence": 0.99}',
        ):
            c = MailClassifier(mlx=_FakeMlx(reply=reply))
            r = await c.classify({"from_addr": "u@x.com", "subject": "s", "body": "b"})
            assert r["human_queue"] and r["routed_to"] == "human_queue"

    async def test_mlx_failure_fails_open_to_human(self):
        c = MailClassifier(mlx=_FakeMlx(fail=True))
        r = await c.classify({"from_addr": "u@x.com", "subject": "s", "body": "b"})
        assert r["human_queue"] and r["confidence"] == 0.0


def _make_agent(store, imap_results, mlx_reply=_GOOD_REPLY):
    fake = _FakeImap(imap_results)
    poller = MailboxPoller(
        {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX"}, imap_factory=lambda cfg: fake
    )
    return MailboxAgent(store=store, poller=poller, classifier=MailClassifier(mlx=_FakeMlx(reply=mlx_reply)))


class TestStoreAndService:
    async def test_duplicate_message_id_no_duplicate_row(self, store):
        msg = {"message_id": "<dup@x>", "from_addr": "u@x.com", "subject": "s", "received_at": "d", "body": "b"}
        assert await store.record_message(msg) is not None
        assert await store.record_message(msg) is None
        rows = await store.list_since("2000-01-01")
        assert len(rows) == 1

    async def test_classification_roundtrip(self, store):
        msg = {"message_id": "<c@x>", "from_addr": "u@x.com", "subject": "s", "received_at": "d", "body": "b"}
        await store.record_message(msg)
        await store.record_classification(
            "<c@x>",
            {
                "category": "bug",
                "sentiment": "neutral",
                "urgency": "medium",
                "confidence": 0.85,
                "routed_to": "mined_issues+auto_ack",
                "human_queue": False,
                "rule_prefilter": "",
                "model_raw": "{}",
            },
        )
        row = await store.get_by_message_id("<c@x>")
        assert row["category"] == "bug" and row["status"] == "classified" and row["human_queue"] == 0

    def _agent(self, store, imap_results, mlx_reply=_GOOD_REPLY):
        fake = _FakeImap(imap_results)
        poller = MailboxPoller(
            {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX"}, imap_factory=lambda cfg: fake
        )
        return MailboxAgent(store=store, poller=poller, classifier=MailClassifier(mlx=_FakeMlx(reply=mlx_reply)))

    async def test_process_once_end_to_end(self, store):
        agent = self._agent(
            store,
            {
                "uids": b"1 2",
                "fetches": {
                    "1": _rfc822("m1@x", "u@x.com", "Crash", "boom"),
                    "2": _rfc822("m2@x", "no-reply@x.com", "hi", "b"),
                },
            },
        )
        n = await agent.process_once()
        assert n == 2
        assert agent.stats["classified"] == 2
        rows = await store.list_since("2000-01-01")
        assert len(rows) == 2
        # uid checkpoint advanced
        assert await store.get_state(MailboxPoller.state_key()) == "2"

    async def test_crash_recovery_no_duplicates(self, store):
        # same messages refetched (poller restarted, stale uid) — no dup rows
        results = {"uids": b"1", "fetches": {"1": _rfc822("m1@x", "u@x.com", "Crash", "boom")}}
        agent = _make_agent(store, results)
        assert await agent.process_once() == 1
        agent2 = _make_agent(store, results)
        assert await agent2.process_once() == 0
        assert len(await store.list_since("2000-01-01")) == 1

    async def test_poll_failure_recovers_next_cycle(self, store):
        class _Flaky:
            def __init__(self):
                self.n = 0

            def login(self, *a):
                self.n += 1
                if self.n == 1:
                    raise RuntimeError("flaky imap")
                self.logged_in = True

            def select(self, folder, readonly=True):
                return "OK", [b"1"]

            def logout(self):
                pass

            def uid(self, cmd, *args):
                if cmd == "search":
                    return "OK", [b"1"]
                return "OK", [(b"1 (RFC822 {1}", _rfc822("m@x", "u@x.com", "S", "B"))]

        flaky = _Flaky()
        agent = MailboxAgent(
            store=store,
            poller=MailboxPoller(
                {"host": "h", "port": 993, "user": "u", "password": "p", "folder": "INBOX"},
                imap_factory=lambda cfg: flaky,
            ),
            classifier=MailClassifier(mlx=_FakeMlx(reply=_GOOD_REPLY)),
        )
        assert await agent.process_once() == 0
        assert agent.stats["errors"] == 1
        assert await agent.process_once() == 1

    async def test_digest_renders_and_sends_once(self, store, monkeypatch):
        from fusion_cowork.mailbox import digest as digest_mod

        monkeypatch.setattr(
            digest_mod,
            "smtp_config_from_env",
            lambda: {"host": "h", "port": 587, "user": "", "password": "", "from_addr": "n@x", "tls": True},
        )
        monkeypatch.setattr(digest_mod, "resolve_recipient", lambda: "owner@x")
        now = datetime.now(UTC)
        msg = {
            "message_id": "<d@x>",
            "from_addr": "u@x.com",
            "subject": "Crash bug",
            "received_at": "d",
            "body": "b",
            "created_at": now.isoformat(),
        }
        await store.record_message(msg)
        await store.record_classification(
            "<d@x>",
            {
                "category": "bug",
                "sentiment": "neutral",
                "urgency": "medium",
                "confidence": 0.4,
                "routed_to": "human_queue",
                "human_queue": True,
                "rule_prefilter": "",
                "model_raw": "",
            },
        )
        sent = []

        async def fake_deliver(cfg, to_addr, text):
            sent.append(text)
            return True

        d = DigestBuilder()
        d._deliver = fake_deliver
        assert await d.build_and_send(store, window_hours=24) is True
        assert len(sent) == 1
        assert "Needs attention (1)" in sent[0]
        assert "Human queue" in sent[0]
        assert "P0 observe-only" in sent[0]

    def test_digest_without_smtp_is_noop(self, monkeypatch, store):
        monkeypatch.delenv("SMTP_HOST", raising=False)
        d = DigestBuilder()
        assert asyncio.run(d.build_and_send(store)) is False


class TestServiceLifecycle:
    async def test_from_env_disabled_and_enabled(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FUSION_MAILBOX_ENABLED", raising=False)
        assert MailboxAgent.from_env() is None
        monkeypatch.setenv("FUSION_MAILBOX_ENABLED", "1")
        monkeypatch.delenv("FUSION_MAILBOX_IMAP_HOST", raising=False)
        assert MailboxAgent.from_env() is None  # IMAP config incomplete
        monkeypatch.setenv("FUSION_MAILBOX_IMAP_HOST", "h")
        monkeypatch.setenv("FUSION_MAILBOX_IMAP_USER", "u")
        monkeypatch.setenv("FUSION_MAILBOX_IMAP_PASS", "p")
        monkeypatch.setenv("FUSION_COWORK_HOME", str(tmp_path))
        agent = MailboxAgent.from_env(mlx=_FakeMlx(reply=_GOOD_REPLY))
        assert agent is not None
        await agent.stop()

    async def test_start_stop_roundtrip(self, store, tmp_path):
        agent = _make_agent(store, {"uids": b"", "fetches": {}})
        await agent.start()
        assert agent._task is not None
        await asyncio.sleep(0.05)
        await agent.stop()
        assert agent._task is None

    async def test_maybe_digest_rate_limit(self, store, monkeypatch):
        from fusion_cowork.mailbox import digest as digest_mod

        monkeypatch.setattr(
            digest_mod,
            "smtp_config_from_env",
            lambda: {"host": "h", "port": 587, "user": "", "password": "", "from_addr": "n@x", "tls": True},
        )
        monkeypatch.setattr(digest_mod, "resolve_recipient", lambda: "owner@x")
        agent = _make_agent(store, {"uids": b"", "fetches": {}})
        sent = []

        async def fake_deliver(cfg, to_addr, text):
            sent.append(text)
            return True

        agent._digest._deliver = fake_deliver
        now = datetime.now(UTC)
        out = await agent.maybe_digest(now - timedelta(hours=25))
        assert len(sent) == 1 and (out - now).total_seconds() < 60
        await agent.maybe_digest(out)
        assert len(sent) == 1  # within window, no second send
