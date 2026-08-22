"""compute_client_delta() is pure dict/JSON diffing with no subprocess or
network calls — cheap to pin down, and it's the crux of the hot-reload
path that lets a user add/remove avoid a full Xray restart.
"""
import xray_hot_reload as hr


def _config(clients_a, clients_b=None, port=443):
    inbounds = [
        {"tag": "a", "protocol": "vless", "port": port,
         "settings": {"clients": clients_a}},
    ]
    if clients_b is not None:
        inbounds.append({
            "tag": "b", "protocol": "vless", "port": port + 1,
            "settings": {"clients": clients_b},
        })
    return {"inbounds": inbounds, "outbounds": [{"tag": "direct"}]}


def test_identical_configs_yield_empty_delta():
    clients = [{"email": "u1", "id": "uuid-1"}]
    old = _config(clients)
    new = _config(list(clients))  # equal by value, different list object

    delta = hr.compute_client_delta(old, new)
    assert delta == {"added": {}, "removed": {}}


def test_added_and_removed_clients_detected():
    old = _config([{"email": "u1", "id": "uuid-1"}])
    new = _config([{"email": "u2", "id": "uuid-2"}])

    delta = hr.compute_client_delta(old, new)
    assert delta["removed"] == {"a": ["u1"]}
    assert delta["added"] == {"a": [{"email": "u2", "id": "uuid-2"}]}


def test_changed_client_body_appears_as_remove_and_add_for_same_email():
    old = _config([{"email": "u1", "id": "uuid-1"}])
    new = _config([{"email": "u1", "id": "uuid-1-rotated"}])

    delta = hr.compute_client_delta(old, new)
    assert delta["removed"] == {"a": ["u1"]}
    assert delta["added"] == {"a": [{"email": "u1", "id": "uuid-1-rotated"}]}


def test_structural_change_outside_clients_returns_none():
    old = _config([{"email": "u1", "id": "uuid-1"}], port=443)
    new = _config([{"email": "u1", "id": "uuid-1"}], port=8443)

    assert hr.compute_client_delta(old, new) is None


def test_client_without_email_makes_that_inbound_undiffable():
    old = _config([{"id": "uuid-1"}])
    new = _config([{"id": "uuid-1"}, {"email": "u2", "id": "uuid-2"}])

    assert hr.compute_client_delta(old, new) is None


def test_duplicate_email_within_an_inbound_makes_it_undiffable():
    old = _config([{"email": "dup", "id": "uuid-1"}])
    new = _config([{"email": "dup", "id": "uuid-1"}, {"email": "dup", "id": "uuid-2"}])

    assert hr.compute_client_delta(old, new) is None


def test_multiple_inbounds_diffed_independently():
    old = _config([{"email": "a1", "id": "1"}], clients_b=[{"email": "b1", "id": "2"}])
    new = _config([{"email": "a2", "id": "3"}], clients_b=[{"email": "b1", "id": "2"}])

    delta = hr.compute_client_delta(old, new)
    assert delta["removed"] == {"a": ["a1"]}
    assert delta["added"] == {"a": [{"email": "a2", "id": "3"}]}
    assert "b" not in delta["removed"]
    assert "b" not in delta["added"]


def test_expect_removed_treats_not_found_as_success():
    # rmu exits 0 but reports a per-user "not found" for an already-absent
    # user; the desired end state (gone) already holds, so this must not
    # raise.
    stdout = "User ghost not found.\nRemoved 0 user(s) in total."
    hr._expect_removed(stdout, ["ghost"], "a")  # no raise


def test_expect_removed_raises_on_genuine_failure():
    stdout = "some xray error\nRemoved 0 user(s) in total."
    try:
        hr._expect_removed(stdout, ["real-user"], "a")
    except hr.HotReloadError:
        pass
    else:
        raise AssertionError("expected HotReloadError")


def test_expect_total_raises_on_mismatched_count():
    try:
        hr._expect_total("Added 1 user(s) in total.", expected=2, operation="adu")
    except hr.HotReloadError:
        pass
    else:
        raise AssertionError("expected HotReloadError")
