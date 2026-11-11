"""compute_client_delta() is pure dict/JSON diffing with no subprocess or
network calls — cheap to pin down, and it's the crux of the hot-reload
path that lets a user add/remove avoid a full Xray restart.
"""
import xray_hot_reload as hr


def _config(clients_a, clients_b=None, port=443):
    inbounds = [
        {"tag": "a", "protocol": "vless", "port": port,
         "settings": {"clients": list(clients_a)}},
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


def test_strip_clients_does_not_mutate_or_copy_the_client_lists():
    clients = [{"email": "u1", "id": "uuid-1"}]
    config = _config(clients)

    stripped, by_tag = hr._strip_clients(config)

    assert config["inbounds"][0]["settings"] == {"clients": clients}
    assert "clients" not in stripped["inbounds"][0]["settings"]
    assert by_tag["a"] is config["inbounds"][0]["settings"]["clients"]


def test_expect_added_treats_already_exists_as_success():
    stdout = ("add user: 1.a\nrpc error: code = Unknown desc = proxy/vless: User 1.a already exists.\n"
              "add user: 2.b\nresult: ok\nAdded 1 user(s) in total.")
    hr._expect_added(stdout, ["1.a", "2.b"])  # no raise


def test_expect_added_raises_on_genuine_failure():
    stdout = "add user: 1.a\nrpc error: something else\nAdded 0 user(s) in total."
    try:
        hr._expect_added(stdout, ["1.a"])
    except hr.HotReloadError:
        pass
    else:
        raise AssertionError("expected HotReloadError")


# --- reconcile_users()/try_hot_reload()/sync_users() against a fake live Xray ---

class FakeXray:
    """Stands in for _run_xray_api(): keeps a per-inbound user list like a
    running Xray and answers `inbounduser`/`rmu`/`adu` with the real CLI's
    output format (captured from Xray v26.7.28)."""

    def __init__(self, live, not_user_managers=()):
        self.live = {tag: list(emails) for tag, emails in live.items()}
        self.not_user_managers = set(not_user_managers)
        self.calls = []
        self.fail_adu = False

    def __call__(self, args, payload=None, timeout=3):
        import json
        self.calls.append(args[0])
        if args[0] == "inbounduser":
            tag = args[1].split("=", 1)[1]
            if tag in self.not_user_managers or tag not in self.live:
                raise hr.HotReloadError("xray api inbounduser failed: proxy is not a UserManager")
            users = [{"account": {}, "email": email} for email in self.live[tag]]
            return json.dumps({"users": users} if users else {})
        if args[0] == "rmu":
            tag = args[1].split("=", 1)[1]
            out, removed = [], 0
            for email in args[2:]:
                out.append(f"remove user: {email}")
                if email in self.live[tag]:
                    self.live[tag].remove(email)
                    removed += 1
                else:
                    out.append(f"rpc error: code = Unknown desc = proxy/vless: User {email} not found.")
            out.append(f"Removed {removed} user(s) in total.")
            return "\n".join(out)
        if args[0] == "adu":
            if self.fail_adu:
                return "Added 0 user(s) in total."
            out, added = [], 0
            for inbound in json.loads(payload)["inbounds"]:
                assert inbound.get("protocol"), "adu needs a complete inbound definition"
                out.append(f"processing inbound: {inbound['tag']}")
                for client in inbound["settings"]["clients"]:
                    out.append(f"add user: {client['email']}")
                    if client["email"] in self.live[inbound["tag"]]:
                        out.append("rpc error: code = Unknown desc = proxy/vless: "
                                   f"User {client['email']} already exists.")
                    else:
                        self.live[inbound["tag"]].append(client["email"])
                        out.append("result: ok")
                        added += 1
            out.append(f"Added {added} user(s) in total.")
            return "\n".join(out)
        raise AssertionError(f"unexpected xray api call: {args}")


class _Core:
    version = "26.7.28"
    started = True

    def __init__(self, config):
        self.config = config


def _use_fake_xray(monkeypatch, fake):
    monkeypatch.setattr(hr, "_run_xray_api", fake)
    monkeypatch.setattr(hr, "_cli_supports_user_ops", lambda version: True)
    monkeypatch.setattr(hr, "XRAY_HOT_RELOAD_ENABLED", True)


def _c(email, uid=None):
    return {"email": email, "id": uid or f"uuid-{email}"}


def test_reconcile_removes_live_users_the_node_never_tracked(monkeypatch):
    # "9.ghost" was added by the panel over gRPC and its removal got lost:
    # core.config never knew it, so a diff against core.config can't see it.
    core = _Core(_config([_c("1.a")]))
    fake = FakeXray({"a": ["1.a", "9.ghost"]})
    _use_fake_xray(monkeypatch, fake)

    stats = hr.reconcile_users(core, {"a": [_c("1.a")]})

    assert fake.live["a"] == ["1.a"]
    assert (stats["added"], stats["removed"], stats["unchanged"]) == (0, 1, 1)


def test_reconcile_restores_a_tracked_user_missing_from_the_live_process(monkeypatch):
    core = _Core(_config([_c("1.a"), _c("2.b")]))
    fake = FakeXray({"a": ["1.a"]})
    _use_fake_xray(monkeypatch, fake)

    stats = hr.reconcile_users(core, {"a": [_c("1.a"), _c("2.b")]})

    assert sorted(fake.live["a"]) == ["1.a", "2.b"]
    assert (stats["added"], stats["removed"]) == (1, 0)


def test_reconcile_repushes_a_live_user_whose_credentials_are_unverified(monkeypatch):
    # Live but untracked (pushed by the panel directly): its credentials
    # may be stale, so it is removed and re-added with the wanted entry.
    core = _Core(_config([]))
    fake = FakeXray({"a": ["1.a"]})
    _use_fake_xray(monkeypatch, fake)

    stats = hr.reconcile_users(core, {"a": [_c("1.a", "rotated")]})

    assert fake.calls == ["inbounduser", "rmu", "adu"]
    assert (stats["added"], stats["removed"]) == (1, 1)
    assert core.config["inbounds"][0]["settings"]["clients"] == [_c("1.a", "rotated")]


def test_reconcile_is_a_no_op_when_live_tracked_and_wanted_agree(monkeypatch):
    core = _Core(_config([_c("1.a"), _c("2.b")]))
    fake = FakeXray({"a": ["1.a", "2.b"]})
    _use_fake_xray(monkeypatch, fake)

    stats = hr.reconcile_users(core, {"a": [_c("1.a"), _c("2.b")]})

    assert fake.calls == ["inbounduser"]
    assert stats == {"added": 0, "removed": 0, "unchanged": 2, "skipped_tags": []}


def test_reconcile_collapses_duplicate_live_entries(monkeypatch):
    # A classic Shadowsocks inbound accepts the same email twice.
    core = _Core(_config([_c("1.a")]))
    fake = FakeXray({"a": ["1.a", "1.a"]})
    _use_fake_xray(monkeypatch, fake)

    hr.reconcile_users(core, {"a": [_c("1.a")]})

    assert fake.live["a"] == ["1.a"]


def test_reconcile_matches_emails_case_insensitively(monkeypatch):
    core = _Core(_config([_c("1.Alice")]))
    fake = FakeXray({"a": ["1.alice"]})
    _use_fake_xray(monkeypatch, fake)

    stats = hr.reconcile_users(core, {"a": [_c("1.Alice")]})

    assert fake.calls == ["inbounduser"]
    assert stats["unchanged"] == 1


def test_reconcile_skips_unknown_and_protected_tags(monkeypatch):
    core = _Core(_config([_c("1.a")]))
    fake = FakeXray({"a": ["1.a"]})
    _use_fake_xray(monkeypatch, fake)

    stats = hr.reconcile_users(core, {"a": [_c("1.a")], "nope": [_c("1.a")], "API_INBOUND": []})

    assert sorted(stats["skipped_tags"]) == ["API_INBOUND", "nope"]


def test_reconcile_skips_an_unchanged_inbound_that_is_not_user_manageable(monkeypatch):
    core = _Core(_config([_c("1.a")]))
    fake = FakeXray({}, not_user_managers={"a"})
    _use_fake_xray(monkeypatch, fake)

    stats = hr.reconcile_users(core, {"a": [_c("1.a")]})

    assert stats["added"] == stats["removed"] == 0


def test_reconcile_fails_when_a_non_user_manageable_inbound_would_change(monkeypatch):
    core = _Core(_config([_c("1.a")]))
    _use_fake_xray(monkeypatch, FakeXray({}, not_user_managers={"a"}))

    try:
        hr.reconcile_users(core, {"a": [_c("2.b")]})
    except hr.HotReloadError:
        pass
    else:
        raise AssertionError("expected HotReloadError")


def test_reconcile_rejects_a_client_list_with_repeated_emails(monkeypatch):
    core = _Core(_config([]))
    _use_fake_xray(monkeypatch, FakeXray({"a": []}))

    try:
        hr.reconcile_users(core, {"a": [_c("1.a"), _c("1.A")]})
    except hr.HotReloadError:
        pass
    else:
        raise AssertionError("expected HotReloadError")


def test_reconcile_chunks_large_removals(monkeypatch):
    emails = [f"{i}.u" for i in range(5)]
    core = _Core(_config([_c(e) for e in emails]))
    fake = FakeXray({"a": emails})
    _use_fake_xray(monkeypatch, fake)
    monkeypatch.setattr(hr, "_RMU_CHUNK", 2)

    stats = hr.reconcile_users(core, {"a": []})

    assert fake.calls.count("rmu") == 3
    assert fake.live["a"] == []
    assert stats["removed"] == 5


def test_reconcile_chunks_large_additions(monkeypatch):
    core = _Core(_config([], []))
    fake = FakeXray({"a": [], "b": []})
    _use_fake_xray(monkeypatch, fake)
    monkeypatch.setattr(hr, "_ADU_CHUNK", 2)

    stats = hr.reconcile_users(core, {"a": [_c(f"{i}.u") for i in range(3)], "b": [_c("9.z")]})

    assert fake.calls.count("adu") == 2
    assert len(fake.live["a"]) == 3 and fake.live["b"] == ["9.z"]
    assert stats["added"] == 4


def test_failed_add_leaves_tracked_config_without_the_unapplied_users(monkeypatch):
    core = _Core(_config([_c("1.a")]))
    fake = FakeXray({"a": ["1.a"]})
    fake.fail_adu = True
    _use_fake_xray(monkeypatch, fake)

    try:
        hr.reconcile_users(core, {"a": [_c("1.a"), _c("2.b")]})
    except hr.HotReloadError:
        pass
    else:
        raise AssertionError("expected HotReloadError")
    assert core.config["inbounds"][0]["settings"]["clients"] == [_c("1.a")]


def test_try_hot_reload_heals_drift_even_when_configs_are_identical(monkeypatch):
    config = _config([_c("1.a")])
    core = _Core(config)
    fake = FakeXray({"a": ["1.a", "9.ghost"]})
    _use_fake_xray(monkeypatch, fake)
    new_config = _config([_c("1.a")])

    assert hr.try_hot_reload(core, new_config) is True
    assert fake.live["a"] == ["1.a"]
    assert core.config is new_config


def test_try_hot_reload_declines_structural_changes_without_touching_xray(monkeypatch):
    core = _Core(_config([_c("1.a")], port=443))
    fake = FakeXray({"a": ["1.a"]})
    _use_fake_xray(monkeypatch, fake)

    assert hr.try_hot_reload(core, _config([_c("1.a")], port=8443)) is False
    assert fake.calls == []


def test_try_hot_reload_falls_back_when_the_apply_fails(monkeypatch):
    core = _Core(_config([_c("1.a")]))
    fake = FakeXray({"a": ["1.a"]})
    fake.fail_adu = True
    _use_fake_xray(monkeypatch, fake)

    assert hr.try_hot_reload(core, _config([_c("1.a"), _c("2.b")])) is False


def test_try_hot_reload_without_cli_support_still_no_ops_identical_configs(monkeypatch):
    core = _Core(_config([_c("1.a")]))
    monkeypatch.setattr(hr, "XRAY_HOT_RELOAD_ENABLED", True)
    monkeypatch.setattr(hr, "_cli_supports_user_ops", lambda version: False)

    assert hr.try_hot_reload(core, _config([_c("1.a")])) is True
    assert hr.try_hot_reload(core, _config([_c("2.b")])) is False


def test_sync_users_refuses_when_hot_ops_are_unavailable(monkeypatch):
    core = _Core(_config([]))
    monkeypatch.setattr(hr, "XRAY_HOT_RELOAD_ENABLED", False)

    try:
        hr.sync_users(core, {"a": []})
    except hr.HotReloadUnavailable:
        pass
    else:
        raise AssertionError("expected HotReloadUnavailable")


def test_can_keep_core_requires_a_running_core_and_usable_hot_ops(monkeypatch):
    core = _Core(_config([]))
    monkeypatch.setattr(hr, "XRAY_HOT_RELOAD_ENABLED", True)
    monkeypatch.setattr(hr, "_cli_supports_user_ops", lambda version: True)
    assert hr.can_keep_core(core) is True

    core.started = False
    assert hr.can_keep_core(core) is False

    core.started = True
    monkeypatch.setattr(hr, "_cli_supports_user_ops", lambda version: False)
    assert hr.can_keep_core(core) is False


def test_unreconcilable_client_list_is_left_alone_when_unchanged(monkeypatch):
    # A preconfigured client without an email can't be reconciled, but an
    # unchanged inbound must not turn a hot reload into a full restart.
    static = [{"id": "static-no-email"}]
    core = _Core(_config(static, [_c("1.a")]))
    fake = FakeXray({"a": [], "b": ["1.a"]})
    _use_fake_xray(monkeypatch, fake)

    assert hr.try_hot_reload(core, _config(static, [_c("1.a"), _c("2.b")])) is True
    assert sorted(fake.live["b"]) == ["1.a", "2.b"]
    assert fake.live["a"] == []


def test_unreconcilable_client_list_that_changed_forces_a_full_restart(monkeypatch):
    core = _Core(_config([{"id": "static-no-email"}]))
    _use_fake_xray(monkeypatch, FakeXray({"a": []}))

    assert hr.try_hot_reload(core, _config([{"id": "another-no-email"}])) is False


def test_replacements_are_applied_in_small_remove_then_add_steps(monkeypatch):
    # Untracked live users get re-pushed; doing it in small steps keeps the
    # window in which they can't open new connections short.
    emails = [f"{i}.u" for i in range(5)]
    core = _Core(_config([]))
    fake = FakeXray({"a": emails})
    _use_fake_xray(monkeypatch, fake)
    monkeypatch.setattr(hr, "_REPLACE_CHUNK", 2)

    stats = hr.reconcile_users(core, {"a": [_c(e) for e in emails]})

    assert fake.calls == ["inbounduser", "rmu", "adu", "rmu", "adu", "rmu", "adu"]
    assert sorted(fake.live["a"]) == emails
    assert (stats["added"], stats["removed"]) == (5, 5)
