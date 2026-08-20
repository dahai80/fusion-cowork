from __future__ import annotations

import time

from fusion_cowork.space.presence import PresenceManager, PresenceState


def _noop_emitter():
    class _E:
        def create_event(self, *a, **k):
            pass

    return _E()


def test_heartbeat_marks_online():
    pm = PresenceManager(event_emitter=_noop_emitter())
    st = pm.heartbeat("s1", "u1", display_name="Alice")
    assert st.online is True
    assert st.user_id == "u1"
    assert st.display_name == "Alice"
    assert st.last_heartbeat > 0


def test_set_cursor_updates_position():
    pm = PresenceManager(event_emitter=_noop_emitter())
    st = pm.set_cursor("s1", "u1", 12.5, 30, target="artifact_1")
    assert st.cursor_x == 12.5
    assert st.cursor_y == 30.0
    assert st.cursor_target == "artifact_1"
    assert st.online is True


def test_list_present_returns_states():
    pm = PresenceManager(event_emitter=_noop_emitter())
    pm.heartbeat("s1", "u1")
    pm.heartbeat("s1", "u2", display_name="Bob")
    states = pm.list_present("s1")
    assert len(states) == 2
    ids = {s.user_id for s in states}
    assert ids == {"u1", "u2"}


def test_timeout_marks_offline():
    pm = PresenceManager(event_emitter=_noop_emitter(), timeout=0.05)
    pm.heartbeat("s1", "u1")
    time.sleep(0.06)
    states = pm.list_present("s1")
    assert states[0].online is False


def test_get_returns_none_for_unknown():
    pm = PresenceManager(event_emitter=_noop_emitter())
    assert pm.get("s1", "u1") is None
    pm.heartbeat("s1", "u1")
    assert pm.get("s1", "u1") is not None
    assert pm.get("s1", "u2") is None


def test_remove_user():
    pm = PresenceManager(event_emitter=_noop_emitter())
    pm.heartbeat("s1", "u1")
    assert pm.remove("s1", "u1") is True
    assert pm.remove("s1", "u1") is False
    assert pm.list_present("s1") == []


def test_extras_merge():
    pm = PresenceManager(event_emitter=_noop_emitter())
    pm.heartbeat("s1", "u1", extras={"device": "mac"})
    pm.heartbeat("s1", "u1", extras={"page": "home"})
    st = pm.get("s1", "u1")
    assert st.extras["device"] == "mac"
    assert st.extras["page"] == "home"


def test_no_emitter_silent():
    pm = PresenceManager(event_emitter=None)
    st = pm.heartbeat("s1", "u1")
    assert isinstance(st, PresenceState)
    states = pm.list_present("s1")
    assert len(states) == 1


def test_presence_state_to_dict_keys():
    st = PresenceState(user_id="u1", display_name="A", online=True)
    d = st.to_dict()
    assert d["user_id"] == "u1"
    assert "cursor_x" in d and "cursor_y" in d and "last_heartbeat" in d


def test_cursor_without_prior_heartbeat():
    pm = PresenceManager(event_emitter=_noop_emitter())
    st = pm.set_cursor("s1", "newuser", 1, 2)
    assert st.cursor_x == 1
    assert st.online is True
    assert pm.get("s1", "newuser") is not None
