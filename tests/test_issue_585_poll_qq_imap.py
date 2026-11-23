# issue #585: QQ IMAP poll — FakeIMAP4SSL (monkeypatched IMAP4_SSL) covers the
# poller protocol path without real TLS; cursor resume, Message-ID dedupe,
# HTML strip, attachment skip, node wiring, password never leaks.


import pytest

from fusion_cowork.demand.imap_poll import (
    ImapConfig,
    RawMail,
    parse_message,
    poll_once_sync,
    raw_mail_to_record,
    strip_html,
)
from fusion_cowork.demand.store import LeadStore
from fusion_cowork.engine.node import NodeConfig, NodeStatus
from fusion_cowork.nodes.demand.demand_nodes import PollQqImapNode


def _rfc822(message_id: str, subject: str, body: str, html: str = "") -> bytes:
    parts = [
        f"Message-ID: {message_id}",
        f"Subject: {subject}",
        "From: Sender <sender@example.com>",
        "Date: Sat, 22 Nov 2026 10:00:00 +0800",
        "MIME-Version: 1.0",
    ]
    if html:
        parts.append('Content-Type: multipart/alternative; boundary="BB"')
        head = "\r\n".join(parts)
        return (
            head
            + "\r\n\r\n--BB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            + body
            + "\r\n--BB\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            + html
            + "\r\n--BB--\r\n"
        ).encode("utf-8")
    parts.append('Content-Type: text/plain; charset="utf-8"')
    head = "\r\n".join(parts)
    return (head + "\r\n\r\n" + body + "\r\n").encode("utf-8")


def _rfc822_attachment(message_id: str) -> bytes:
    head = "\r\n".join(
        [
            f"Message-ID: {message_id}",
            "Subject: with attachment",
            "From: a@example.com",
            "MIME-Version: 1.0",
            'Content-Type: multipart/mixed; boundary="MM"',
        ]
    )
    return (
        head
        + "\r\n\r\n--MM\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nreal body here\r\n"
        + "--MM\r\nContent-Type: application/pdf; name=evil.pdf\r\nContent-Disposition: attachment; filename=evil.pdf\r\n\r\n%PDF-1.4 payload\r\n--MM--\r\n"
    ).encode("utf-8")


class FakeIMAP4SSL:
    # minimal IMAP4_SSL stand-in: mailbox served from _MAILBOX dict
    mailbox: dict = {}
    fail_login: bool = False

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.selected = False
        self.closed = False

    def login(self, user: str, password: str):
        if FakeIMAP4SSL.fail_login:
            raise RuntimeError("login rejected")
        return ("OK", [b"Logged in"])

    def select(self, folder: str, readonly: bool = False):
        self.selected = True
        assert readonly is True  # poller must be read-only
        return ("OK", [b"1"])

    def uid(self, command: str, *args):
        if command == "search":
            rng = args[-1]  # "UID <last>:*"
            last = int(rng.split(":")[0].split()[-1])
            uids = [u for u in sorted(FakeIMAP4SSL.mailbox) if u > last]
            return ("OK", [b" ".join(str(u).encode() for u in uids)])
        if command == "fetch":
            uid = int(args[0])
            raw = FakeIMAP4SSL.mailbox.get(uid)
            if raw is None:
                return ("OK", [None])
            return ("OK", [(b"1 (UID %d BODY[] {%d}" % (uid, len(raw)), raw), b")"])
        return ("BAD", [b"unknown"])

    def logout(self):
        self.closed = True
        return ("BYE", [b""])


@pytest.fixture
def fake_imap(monkeypatch):
    FakeIMAP4SSL.mailbox = {}
    FakeIMAP4SSL.fail_login = False
    import fusion_cowork.demand.imap_poll as mod

    monkeypatch.setattr(mod, "IMAP4_SSL", FakeIMAP4SSL)
    return FakeIMAP4SSL


def _cfg() -> ImapConfig:
    return ImapConfig(host="fake", port=993, user="u@example.com", password="SECRET-PW", folder="INBOX")


def test_strip_html_strips_tags_and_script():
    html = "<html><script>alert(1)</script><style>.x{}</style><p>hello <b>world</b></p><div>line2</div></html>"
    text = strip_html(html)
    assert "alert" not in text
    assert ".x" not in text
    assert "hello" in text and "world" in text and "line2" in text


def test_parse_message_prefers_plain_and_decodes():
    mail = parse_message(_rfc822("<m1@x>", "subject one", "plain body", "<p>html body</p>"), 5, "INBOX")
    assert mail.message_id == "<m1@x>"
    assert mail.subject == "subject one"
    assert "plain body" in mail.body_text
    assert "html body" not in mail.body_text
    assert mail.arrived_at > 0


def test_parse_message_attachment_never_opened():
    mail = parse_message(_rfc822_attachment("<m2@x>"), 6, "INBOX")
    assert "real body here" in mail.body_text
    assert "%PDF" not in mail.body_text  # attachment payload never decoded


def test_parse_message_synthetic_id_when_missing():
    raw = _rfc822("", "no id", "body").replace(b"Message-ID: ", b"")
    mail = parse_message(raw, 7, "INBOX")
    assert mail.message_id.startswith("synthetic-7-")


def test_poll_once_fetches_new_and_advances_cursor(fake_imap):
    fake_imap.mailbox = {5: _rfc822("<a@x>", "s5", "body5"), 9: _rfc822("<b@x>", "s9", "body9")}
    result = poll_once_sync(_cfg(), last_uid=0)
    assert result["error"] == ""
    assert [m.message_id for m in result["mails"]] == ["<a@x>", "<b@x>"]
    assert result["max_uid"] == 9
    # cursor advanced: re-poll with last_uid=9 -> nothing (never re-emits)
    result2 = poll_once_sync(_cfg(), last_uid=9)
    assert result2["mails"] == []
    assert result2["max_uid"] == 9


def test_poll_once_error_no_password_leak(fake_imap):
    fake_imap.fail_login = True
    result = poll_once_sync(_cfg(), last_uid=0)
    assert result["error"]
    assert "SECRET-PW" not in result["error"]


async def test_node_poll_and_dedupe(tmp_path, fake_imap, monkeypatch):
    fake_imap.mailbox = {3: _rfc822("<dup@x>", "s3", "body3"), 4: _rfc822("<dup@x>", "s3", "body3")}
    monkeypatch.setenv("FUSION_QQ_IMAP_USER", "u@example.com")
    monkeypatch.setenv("FUSION_QQ_IMAP_PASSWORD", "SECRET-PW")
    db = str(tmp_path / "demand.db")

    node = PollQqImapNode("n1", NodeConfig(params={"db_path": db}))
    r1 = await node.execute({})
    assert r1.status == NodeStatus.SUCCESS
    # same Message-ID twice in one batch -> one record, one duplicate
    assert r1.data["new"] == 1
    assert r1.data["duplicates"] == 1
    assert r1.data["cursor"] == 4

    # re-run with empty mailbox: cursor prevents refetch, zero duplicates
    r2 = await node.execute({})
    assert r2.data["new"] == 0
    assert r2.data["duplicates"] == 0

    store = LeadStore(db)
    await store.connect()
    rows = await store.list_unnormalized()
    await store.close()
    assert len(rows) == 1
    assert rows[0]["subject"] == "s3"


async def test_node_upserts_dedupe_across_cursors(tmp_path, fake_imap, monkeypatch):
    # same Message-ID under a different UID (server duplicate) still deduped
    fake_imap.mailbox = {2: _rfc822("<same@x>", "s", "b")}
    monkeypatch.setenv("FUSION_QQ_IMAP_USER", "u@example.com")
    monkeypatch.setenv("FUSION_QQ_IMAP_PASSWORD", "SECRET-PW")
    db = str(tmp_path / "demand.db")
    node = PollQqImapNode("n1", NodeConfig(params={"db_path": db}))
    assert (await node.execute({})).data["new"] == 1
    fake_imap.mailbox = {8: _rfc822("<same@x>", "s", "b")}
    r = await node.execute({})
    assert r.data["new"] == 0
    assert r.data["duplicates"] == 1


async def test_node_fails_without_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("FUSION_QQ_IMAP_USER", raising=False)
    monkeypatch.delenv("FUSION_QQ_IMAP_PASSWORD", raising=False)
    node = PollQqImapNode("n1", NodeConfig(params={"db_path": str(tmp_path / "d.db")}))
    r = await node.execute({})
    assert r.status == NodeStatus.FAILED
    assert "FUSION_QQ_IMAP" in r.error


async def test_node_poll_failure_visible(tmp_path, fake_imap, monkeypatch):
    fake_imap.fail_login = True
    monkeypatch.setenv("FUSION_QQ_IMAP_USER", "u@example.com")
    monkeypatch.setenv("FUSION_QQ_IMAP_PASSWORD", "SECRET-PW")
    node = PollQqImapNode("n1", NodeConfig(params={"db_path": str(tmp_path / "d.db")}))
    r = await node.execute({})
    assert r.status == NodeStatus.FAILED
    assert "login rejected" in r.error


def test_raw_mail_to_record_fields():
    rec = raw_mail_to_record(
        RawMail(message_id="<x@x>", uid=3, subject="s", from_addr="f", body_text="b", arrived_at=0)
    )
    assert rec["message_id"] == "<x@x>"
    assert rec["uid"] == 3
    assert rec["arrived_at"] > 0  # defaulted to now
