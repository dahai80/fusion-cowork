"""issue #404: HITL email notification — notify registered users at every human touchpoint.

- SMTP_HOST missing → notifier disabled, zero behavior change
- recipient: FUSION_COWORK_NOTIFY_EMAIL override > identity registration (run's identity token → uid → /api/v1/users/{uid} → email)
- identity has no email / token has no uid / identity disabled → WARN and skip, no crash
- fire-and-forget: send on a daemon thread, 3 retries, final failure WARNs, run never blocks
- engine wiring: pending + resolved (resolved requires SMTP_NOTIFY_RESOLVED=1); callback exceptions are caught
- email body: run_id / node / #/pipeline/<run_id> deep link / one-line summary, plain-text English
"""

import logging
import time

import pytest

from fusion_cowork.auth.identity import IdentityClient
from fusion_cowork.engine.workflow import WorkflowEngine
from fusion_cowork.server import email_notify
from fusion_cowork.server.email_notify import HitlEmailNotifier, resolve_recipient, smtp_config_from_env


def _cfg(**over):
    base = {"host": "smtp.test", "port": 2525, "user": "u", "password": "p", "from_addr": "cowork@test", "tls": False}
    base.update(over)
    return base


def _wait_for(cond, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


@pytest.fixture
def no_env(monkeypatch):
    for k in ("SMTP_HOST", "FUSION_COWORK_NOTIFY_EMAIL", "SMTP_NOTIFY_RESOLVED"):
        monkeypatch.delenv(k, raising=False)


class TestConfig:
    def test_no_host_disabled(self, no_env):
        assert smtp_config_from_env() is None
        assert not HitlEmailNotifier(_cfg(host="")).enabled

    def test_host_enables_with_defaults(self, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test")
        cfg = smtp_config_from_env()
        assert cfg["port"] == 587 and cfg["tls"] is True
        assert HitlEmailNotifier(cfg).enabled


class TestRecipient:
    def test_env_override_wins(self, monkeypatch, no_env):
        monkeypatch.setenv("FUSION_COWORK_NOTIFY_EMAIL", "ops@test")
        assert resolve_recipient() == "ops@test"

    def test_no_token_skips_with_warn(self, monkeypatch, no_env, caplog):
        from fusion_cowork.auth import identity_propagation

        token = identity_propagation.set_current_identity_token("")
        try:
            with caplog.at_level(logging.WARNING, logger="fusion_cowork.email_notify"):
                assert resolve_recipient() == ""
        finally:
            identity_propagation._IDENTITY_TOKEN.reset(token)
        assert any("no identity token" in r.message for r in caplog.records)

    def test_identity_user_without_email_skips(self, monkeypatch, no_env, caplog):
        import jwt as pyjwt

        from fusion_cowork.auth import identity_propagation

        token = pyjwt.encode({"uid": "u7"}, "k", algorithm="HS256")
        tok = identity_propagation.set_current_identity_token(token)
        monkeypatch.setenv("FUSION_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("FUSION_IDENTITY_SERVICE_TOKEN", "svc")
        client = IdentityClient(base_url="http://identity.test", service_token="svc")

        def _factory(method, url, **kw):
            class R:
                status_code = 200

                def json(self):
                    return {"user": {"name": "u7"}}  # no email field

            return R()

        client._client = type("C", (), {"get": staticmethod(lambda url, **kw: _factory("GET", url, **kw))})()
        monkeypatch.setattr("fusion_cowork.auth.identity.get_identity_client", lambda: client)
        try:
            with caplog.at_level(logging.WARNING, logger="fusion_cowork.email_notify"):
                assert resolve_recipient() == ""
        finally:
            identity_propagation._IDENTITY_TOKEN.reset(tok)
        assert any("no registered email" in r.message for r in caplog.records)


class TestSend:
    @pytest.fixture
    def recipient(self, monkeypatch):
        monkeypatch.setenv("FUSION_COWORK_NOTIFY_EMAIL", "owner@test")

    def test_pending_delivers_plain_text_with_deep_link(self, no_env, recipient):
        sent = []

        def fake_send(cfg, msg):
            sent.append((cfg, msg))

        n = HitlEmailNotifier(_cfg(), base_url="http://w.test", send_fn=fake_send)
        n("run_x", "ba_gate", {}, "pending", "")
        time.sleep(0.5)
        assert len(sent) == 1
        msg = sent[0][1]
        assert "run_x" in msg["Subject"]
        body = msg.get_content()
        assert "#/pipeline/run_x" in body
        assert "ba_gate" in body
        assert "waiting for your approval" in body

    def test_resolved_requires_opt_in(self, no_env, monkeypatch, recipient):
        calls = []
        n = HitlEmailNotifier(_cfg(), send_fn=lambda cfg, msg: calls.append(msg))
        n("run_x", "n", {}, "resolved", "approved")
        _wait_for(lambda: len(calls) > 0, 1.0)
        assert calls == []  # opt-in off
        monkeypatch.setenv("SMTP_NOTIFY_RESOLVED", "1")
        n("run_x", "n", {}, "resolved", "approved")
        # fire-and-forget daemon thread: poll instead of fixed sleep (CI load, py3.13 shards)
        assert _wait_for(lambda: len(calls) == 1, 5.0)
        assert "approved" in calls[0].get_content()

    def test_smtp_down_retries_then_warns_pipeline_continues(self, no_env, caplog, monkeypatch, recipient):
        monkeypatch.setattr(email_notify, "_RETRY_SLEEP", 0.01)
        attempts = []

        def broken(cfg, msg):
            attempts.append(1)
            raise ConnectionRefusedError("smtp down")

        n = HitlEmailNotifier(_cfg(), send_fn=broken)
        with caplog.at_level(logging.WARNING, logger="fusion_cowork.email_notify"):
            n("run_x", "n", {}, "pending", "")
            time.sleep(0.8)
        assert len(attempts) == 3  # 3 attempts, fire-and-forget
        assert any("NOT delivered" in r.message and "pipeline continues" in r.message for r in caplog.records)

    def test_disabled_notifier_is_noop(self, no_env, recipient):
        n = HitlEmailNotifier({"host": ""}, send_fn=lambda cfg, msg: (_ for _ in ()).throw(AssertionError("no send")))
        n("run_x", "n", {}, "pending", "")
        time.sleep(0.2)  # no crash, no thread work


class TestEngineWiring:
    def test_engine_callback_swallows_exceptions(self, no_env):
        eng = WorkflowEngine()

        def boom(*a):
            raise RuntimeError("email subsystem exploded")

        eng.hitl_notifier = boom
        node = type("N", (), {"name": "gate_a"})()
        eng._fire_hitl_email("run_x", node, {}, "pending", "")  # must not raise

    def test_engine_default_none_is_noop(self, no_env):
        eng = WorkflowEngine()
        node = type("N", (), {"name": "gate_a"})()
        eng._fire_hitl_email("run_x", node, {}, "pending", "")  # no callback, no crash

    def test_create_web_app_attaches_notifier_when_smtp_configured(self, no_env, monkeypatch, tmp_path):
        monkeypatch.setenv("SMTP_HOST", "smtp.test")

        from fusion_cowork.server.web_api import create_web_app

        app = create_web_app(store_root=tmp_path / "s")
        eng = app.state.run_registry._engine
        assert eng.hitl_notifier is not None and eng.hitl_notifier.enabled

    def test_create_web_app_no_smtp_no_notifier(self, no_env, tmp_path):

        from fusion_cowork.server.web_api import create_web_app

        app = create_web_app(store_root=tmp_path / "s")
        eng = app.state.run_registry._engine
        assert eng.hitl_notifier is None
