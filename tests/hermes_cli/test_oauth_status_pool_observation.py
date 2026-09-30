"""A status snapshot observes the credential pool; it never leases (refreshes / rotates) an entry.

``get_codex_auth_status`` / ``get_xai_oauth_auth_status`` back every credential-gated listing
(``/model`` picker, ``hermes doctor``, dashboard cards). When that read ran ``pool.select()`` it
refreshed an expiring single-use token, and a *transient* failure of that speculative POST benched
the entry with a persisted cooldown — the picker then rendered the provider as unconfigured
("needs setup" / "0 models") while the runtime resolver kept serving the same credential.

Fixtures adapted from #114379 by @Finn763.
"""

import base64
import json
import time

import pytest

from agent import credential_pool
from agent.credential_pool import load_pool
from hermes_cli.auth import AuthError, DEFAULT_CODEX_BASE_URL, get_codex_auth_status


def _jwt_with_exp(offset_seconds: int) -> str:
    def _b64(payload: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("utf-8")

    return f"{_b64({'alg': 'none'})}.{_b64({'exp': int(time.time()) + offset_seconds})}.sig"


def _pool_only_codex_home(tmp_path, monkeypatch, *, access_tokens: list):
    """HERMES_HOME whose only Codex credentials live in ``credential_pool.openai-codex``; the token
    endpoint is a transient failure (the credential itself is still good)."""
    import hermes_cli.auth as auth
    import hermes_cli.codex_models as codex_models

    home = tmp_path / "hermes"
    home.mkdir()
    entries = [
        {"id": f"codex-pool-entry-{i}", "label": f"device_code-{i}", "auth_type": "oauth", "source": "device_code",
         "priority": i, "request_count": 0, "access_token": token, "refresh_token": f"codex-refresh-token-{i}",
         "base_url": DEFAULT_CODEX_BASE_URL}
        for i, token in enumerate(access_tokens)
    ]
    (home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {"openai-codex": entries}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-cli"))
    monkeypatch.setattr(codex_models, "_fetch_models_from_api", lambda access_token: [])
    refresh_calls: list = []

    def _transient_failure(access_token, refresh_token, *args, **kwargs):
        refresh_calls.append(refresh_token)
        raise AuthError("Codex token refresh failed with status 503.", provider="openai-codex",
                        code="codex_refresh_failed")

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _transient_failure)
    return home, refresh_calls


def _persisted_pool(home) -> list:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))["credential_pool"]["openai-codex"]


def test_status_snapshot_does_not_refresh_or_bench_an_expiring_pool_entry(tmp_path, monkeypatch):
    home, refresh_calls = _pool_only_codex_home(tmp_path, monkeypatch, access_tokens=[_jwt_with_exp(-3600)])

    status = get_codex_auth_status()

    assert refresh_calls == [], "a status read spent the single-use pool refresh token"
    assert status["logged_in"] is True, status
    assert [e.get("last_status") for e in _persisted_pool(home)] == [None]
    assert load_pool("openai-codex").has_available() is True

    from hermes_cli.model_switch import list_authenticated_providers

    rows = [r for r in list_authenticated_providers(current_provider="openai-codex", current_model="gpt-5.6-sol")
            if r["slug"] == "openai-codex"]
    assert rows and rows[0]["total_models"] > 0, rows

    # Control: the runtime lease still refreshes the same entry.
    load_pool("openai-codex").select()
    assert refresh_calls == ["codex-refresh-token-0"]


def test_status_snapshot_leaves_round_robin_order_and_counts_untouched(tmp_path, monkeypatch):
    home, _ = _pool_only_codex_home(
        tmp_path, monkeypatch, access_tokens=[_jwt_with_exp(3600), _jwt_with_exp(3600)])
    monkeypatch.setattr(credential_pool, "get_pool_strategy", lambda provider, **kwargs: credential_pool.STRATEGY_ROUND_ROBIN)
    before = _persisted_pool(home)

    assert get_codex_auth_status()["logged_in"] is True
    assert _persisted_pool(home) == before, "a status read rotated or re-counted the persisted pool"

    # Control: a runtime selection still rotates and persists the new order.
    load_pool("openai-codex").select()
    assert _persisted_pool(home) != before


def test_read_only_resolver_never_probes_or_mutates_an_exhausted_pool(tmp_path, monkeypatch):
    import hermes_cli.auth_codex as auth_codex
    from hermes_cli.auth import resolve_codex_runtime_credentials

    home, _ = _pool_only_codex_home(
        tmp_path, monkeypatch, access_tokens=[_jwt_with_exp(-3600)])
    payload = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    entry = payload["credential_pool"]["openai-codex"][0]
    entry.update({
        "last_status": "exhausted",
        "last_error_code": 429,
        "last_error_reason": "usage_limit_reached",
        "last_error_reset_at": time.time() + 3600,
    })
    (home / "auth.json").write_text(json.dumps(payload), encoding="utf-8")
    before = (home / "auth.json").read_bytes()
    calls = []

    monkeypatch.setattr(
        auth_codex,
        "_probe_codex_pool_entry_quota_restored",
        lambda _entry: calls.append("probe") or True,
    )
    monkeypatch.setattr(
        auth_codex,
        "clear_codex_pool_quota_cooldowns",
        lambda: calls.append("clear") or 1,
    )

    with pytest.raises(AuthError):
        resolve_codex_runtime_credentials(read_only=True)

    assert calls == []
    assert (home / "auth.json").read_bytes() == before
    assert not (home / "auth.lock").exists()


def _singleton_only_codex_home(tmp_path, monkeypatch, *, tokens: dict, codex_cli_tokens: dict):
    """HERMES_HOME whose Codex credentials are the ``providers.openai-codex`` singleton only, with a
    valid Codex CLI login sitting beside it in ``CODEX_HOME``."""
    home, codex_home = tmp_path / "hermes", tmp_path / "codex"
    home.mkdir()
    codex_home.mkdir()
    (home / "auth.json").write_text(json.dumps({
        "version": 1, "active_provider": "openai-codex",
        "providers": {"openai-codex": {"tokens": tokens, "auth_mode": "chatgpt"}}}), encoding="utf-8")
    (codex_home / "auth.json").write_text(json.dumps({"tokens": codex_cli_tokens}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return home


def _singleton_tokens(home) -> dict:
    return json.loads((home / "auth.json").read_text(encoding="utf-8"))["providers"]["openai-codex"]["tokens"]


def test_status_snapshot_never_adopts_codex_cli_tokens(tmp_path, monkeypatch):
    """#68004: a Hermes store missing its refresh_token is recovery-eligible on the runtime path, but
    ``hermes status`` / ``hermes doctor`` must not import the Codex CLI's single-use token family."""
    from hermes_cli.auth import resolve_codex_runtime_credentials

    stale = {"access_token": _jwt_with_exp(-60)}
    home = _singleton_only_codex_home(
        tmp_path, monkeypatch, tokens=stale,
        codex_cli_tokens={"access_token": _jwt_with_exp(86400), "refresh_token": "cli-refresh"})

    get_codex_auth_status()

    assert _singleton_tokens(home) == stale, "a status read persisted the Codex CLI login into auth.json"

    # Control: the runtime resolver still self-heals from the CLI file.
    assert resolve_codex_runtime_credentials()["source"] == "hermes-auth-store"
    assert _singleton_tokens(home)["refresh_token"] == "cli-refresh"


def test_status_snapshot_never_refreshes_an_expired_singleton(tmp_path, monkeypatch):
    """#68004: an expired singleton token is reported as stored; only the runtime lease may spend
    the refresh token (and ``read_only`` wins over ``force_refresh``).

    The observational pool leaves a missing pool absent and reports the stored singleton
    through the read-only resolver, without seeding a new device-code row."""
    import hermes_cli.auth as auth
    from hermes_cli.auth import resolve_codex_runtime_credentials

    expired = {"access_token": _jwt_with_exp(-60), "refresh_token": "singleton-refresh"}
    home = _singleton_only_codex_home(
        tmp_path, monkeypatch, tokens=expired, codex_cli_tokens={})
    refresh_calls: list = []

    def _rotate(access_token, refresh_token, *args, **kwargs):
        refresh_calls.append(refresh_token)
        return {"access_token": _jwt_with_exp(86400), "refresh_token": "rotated-refresh"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rotate)

    status = get_codex_auth_status()

    assert refresh_calls == [], "a status read spent the single-use singleton refresh token"
    assert status["logged_in"] is True and status["api_key"] == expired["access_token"]
    assert status["source"] == "hermes-auth-store", "the status read never reached the singleton resolver"
    assert _singleton_tokens(home) == expired

    # Secondary: read_only wins over force_refresh on the resolver itself.
    resolve_codex_runtime_credentials(force_refresh=True, read_only=True)
    assert refresh_calls == [] and _singleton_tokens(home) == expired

    # Control: the runtime path refreshes and persists the rotated pair.
    resolve_codex_runtime_credentials()
    assert refresh_calls == ["singleton-refresh"]
    assert _singleton_tokens(home)["refresh_token"] == "rotated-refresh"


def test_status_snapshot_leaves_the_auth_store_manifest_byte_identical(tmp_path, monkeypatch):
    """#68004: from the first snapshot, a status read creates no ``auth.lock`` and
    rewrites no byte of ``auth.json`` — the read is lock-free because the writer replaces the file
    atomically."""
    expired = {"access_token": _jwt_with_exp(-60), "refresh_token": "singleton-refresh"}
    home = _singleton_only_codex_home(tmp_path, monkeypatch, tokens=expired, codex_cli_tokens={})

    manifest = {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()}

    status = get_codex_auth_status()

    assert status["source"] == "hermes-auth-store"
    assert {p.name: p.read_bytes() for p in home.iterdir() if p.is_file()} == manifest


def test_model_picker_catalog_never_refreshes_the_stored_codex_login(tmp_path, monkeypatch):
    """#68004: ``/model`` reports the stored login as-is — an expired token means the hardcoded
    catalog, not a spent refresh token."""
    import hermes_cli.auth as auth
    import hermes_cli.codex_models as codex_models
    from hermes_cli.models import _codex_catalog

    expired = {"access_token": _jwt_with_exp(-60), "refresh_token": "singleton-refresh"}
    home = _singleton_only_codex_home(tmp_path, monkeypatch, tokens=expired, codex_cli_tokens={})
    refresh_calls: list = []
    api_tokens: list = []

    def _rotate(access_token, refresh_token, *args, **kwargs):
        refresh_calls.append(refresh_token)
        return {"access_token": _jwt_with_exp(86400), "refresh_token": "rotated-refresh"}

    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", _rotate)
    monkeypatch.setattr(codex_models, "_fetch_models_from_api", lambda token: api_tokens.append(token) or [])

    models = _codex_catalog("openai-codex", False)

    assert models, "the hardcoded catalog is the fallback for an expired stored token"
    assert refresh_calls == [] and api_tokens == []
    assert _singleton_tokens(home) == expired


@pytest.fixture
def observed_auth_store(tmp_path, monkeypatch):
    """Synthetic stores only; record maintenance at the real persistence seams."""
    from contextlib import contextmanager
    import shutil
    import urllib.request
    import httpx
    import hermes_constants
    import hermes_cli.auth as auth
    import hermes_cli.auth_commands as commands
    import hermes_cli.auth_oauth_grants as grants

    root = tmp_path / "root"
    profile = root / "profiles" / "named"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-external-login"))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
    monkeypatch.setattr(commands, "dispatch_plugin_auth", lambda *args: False)
    auth._global_auth_store_cache = None
    auth._oauth_heal_clean_marks.clear()
    auth._oauth_heal_notices.clear()
    events = []

    @contextmanager
    def fake_lock(*args, **kwargs):
        events.append("auth-lock")
        yield

    save = auth._save_auth_store
    copy = shutil.copy2
    mark = grants._persist_oauth_heal_clean_mark

    def observed_save(*args, **kwargs):
        events.append("save-store")
        return save(*args, **kwargs)

    def observed_copy(*args, **kwargs):
        events.append("copy-corrupt-store")
        return copy(*args, **kwargs)

    def observed_mark(*args, **kwargs):
        events.append("save-clean-mark")
        return mark(*args, **kwargs)

    def forbidden_external(*args, **kwargs):
        events.append("external-auth-or-network")
        raise AssertionError("status requested an external auth boundary")

    monkeypatch.setattr(auth, "_auth_store_lock", fake_lock)
    monkeypatch.setattr(auth, "_save_auth_store", observed_save)
    monkeypatch.setattr(credential_pool, "_auth_store_lock", fake_lock)
    monkeypatch.setattr(credential_pool, "_save_auth_store", observed_save)
    monkeypatch.setattr(shutil, "copy2", observed_copy)
    monkeypatch.setattr(grants, "_persist_oauth_heal_clean_mark", observed_mark)
    for name in ("refresh_codex_oauth_pure", "_import_codex_cli_tokens",
                 "_probe_codex_quota_restored"):
        monkeypatch.setattr(auth, name, forbidden_external)
    monkeypatch.setattr("hermes_cli.auth_codex._recover_codex_tokens_from_cli", forbidden_external)
    monkeypatch.setattr("hermes_cli.auth_codex._probe_codex_pool_entry_quota_restored", forbidden_external)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden_external)
    monkeypatch.setattr(httpx, "post", forbidden_external)

    def seed(case):
        def row(identity, offset, **extra):
            return {"id": identity, "auth_type": "oauth", "source": "manual:device_code",
                    "priority": 0, "access_token": _jwt_with_exp(offset), "refresh_token": "fixture-only",
                    "base_url": DEFAULT_CODEX_BASE_URL, **extra}

        store = {"version": 1, "providers": {}}
        if case == "singleton":
            store["providers"]["openai-codex"] = {"tokens": {
                "access_token": _jwt_with_exp(-60), "refresh_token": "fixture-only"}}
        elif case == "profile-fork":
            old = row("shared", 3600, source="device_code", expires_at_ms=int((time.time() + 3600) * 1000))
            fresh = row("shared", 7200, source="device_code", expires_at_ms=int((time.time() + 7200) * 1000))
            store["credential_pool"] = {"openai-codex": [old]}
            (profile / "auth.json").write_text(json.dumps({"version": 1, "providers": {},
                "credential_pool": {"openai-codex": [fresh]}}), encoding="utf-8")
            monkeypatch.setenv("HERMES_HOME", str(profile))
        elif case in ("dead", "healthy"):
            entries = [row("first", 3600), row("second", 7200, priority=1)]
            if case == "dead":
                entries[0].update(last_status="dead", last_status_at=time.time() - 25 * 3600)
            store["credential_pool"] = {"openai-codex": entries}
        (root / "auth.json").write_text("{invalid" if case == "corrupt" else json.dumps(store), encoding="utf-8")
        return root, events

    return seed


def _auth_manifest(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("case", ["singleton", "profile-fork", "dead", "corrupt", "healthy"])
@pytest.mark.parametrize("surface", ["handler", "helper"])
def test_status_first_read_observes_without_maintenance(observed_auth_store, capsys, case, surface):
    from types import SimpleNamespace
    from hermes_cli.auth_commands import auth_status_command

    root, events = observed_auth_store(case)
    before = _auth_manifest(root)
    for _ in range(2):
        if surface == "handler":
            auth_status_command(SimpleNamespace(provider="openai-codex"))
            output = capsys.readouterr().out
            logged_in = "openai-codex: logged in" in output
        else:
            status = get_codex_auth_status()
            logged_in = status["logged_in"]
        assert logged_in is (case != "corrupt")
    unchanged = _auth_manifest(root) == before
    assert not events, "status requested persistent maintenance or external authentication"
    assert unchanged, "status changed a synthetic store, lock, clean mark, or pool order"


@pytest.mark.parametrize("case", ["singleton", "profile-fork", "dead", "corrupt"])
def test_runtime_load_retains_maintenance(observed_auth_store, case):
    root, events = observed_auth_store(case)
    before = _auth_manifest(root)
    pool = load_pool("openai-codex")
    pool.peek()
    assert events, "normal runtime stopped requesting its existing maintenance"
    changed = _auth_manifest(root) != before
    assert changed, "normal runtime stopped updating its synthetic store"

@pytest.fixture
def observed_config_home(tmp_path, monkeypatch):
    import hermes_constants
    import hermes_cli.config as config
    import hermes_cli.auth_commands as commands

    home = tmp_path / "uninitialized-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-codex"))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: home)
    monkeypatch.setattr(commands, "dispatch_plugin_auth", lambda *args: False)
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    monkeypatch.setattr(config.managed_scope, "load_managed_config", lambda: {})
    return home, config


@pytest.mark.parametrize("case", ["missing-home", "config", "overlay", "corrupt"])
def test_config_observation_preserves_effective_policy_without_initialization(observed_config_home, monkeypatch, case):
    home, config = observed_config_home
    if case != "missing-home":
        home.mkdir()
        (home / "config.yaml").write_text(
            "model: [unterminated" if case == "corrupt" else
            "model:\n  base_url: https://fixture.invalid/user\nagent:\n  max_turns: 321\n", encoding="utf-8")
    if case == "overlay":
        monkeypatch.setattr(config.managed_scope, "load_managed_config", lambda: {
            "model": {"base_url": "https://fixture.invalid/managed"}, "agent": {"max_turns": 456}})
    before = _auth_manifest(home)
    dirs = tuple(sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_dir()))
    for _ in range(2):
        result = config.load_config_readonly(observe_only=True)
        if case == "corrupt":
            assert isinstance(result, config.FailedConfigRead)
        elif case in ("config", "overlay"):
            expected = "managed" if case == "overlay" else "user"
            assert result["model"]["base_url"] == f"https://fixture.invalid/{expected}"
            assert result["agent"]["max_turns"] == (456 if case == "overlay" else 321)
    assert _auth_manifest(home) == before
    assert tuple(sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_dir())) == dirs
    assert not config._LOAD_CONFIG_CACHE and not config._LAST_EXPANDED_CONFIG_BY_PATH
    if case == "missing-home":
        assert not home.exists()


@pytest.mark.parametrize("with_config", [False, True])
def test_observation_does_not_suppress_later_runtime_initialization(observed_config_home, with_config):
    home, config = observed_config_home
    if with_config:
        home.mkdir()
        (home / "config.yaml").write_text("agent:\n  max_turns: 321\n", encoding="utf-8")
    config.load_config_readonly(observe_only=True)
    assert not (home / "SOUL.md").exists()
    config.load_config()
    assert (home / "SOUL.md").exists()
    if with_config:
        assert any((home / "backups" / "config").rglob("*"))


@pytest.mark.parametrize("overlay", [False, True])
def test_status_route_keeps_config_and_explicit_override_without_backups(observed_auth_store, monkeypatch, overlay):
    from hermes_cli import config
    from hermes_cli.auth_codex import _codex_pool_route_base_url

    root, events = observed_auth_store("healthy")
    (root / "config.yaml").write_text("model:\n  provider: openai-codex\n  base_url: https://fixture.invalid/user\n", encoding="utf-8")
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    monkeypatch.setattr(config.managed_scope, "load_managed_config", lambda: (
        {"model": {"base_url": "https://fixture.invalid/managed"}} if overlay else {}))
    before = _auth_manifest(root)
    expected = "https://fixture.invalid/managed" if overlay else "https://fixture.invalid/user"
    assert get_codex_auth_status()["base_url"] == expected
    assert _codex_pool_route_base_url("https://fixture.invalid/owned", read_only=True) == "https://fixture.invalid/owned"
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    scope = set_secret_scope({"HERMES_CODEX_BASE_URL": "https://fixture.invalid/explicit"}, profile_home=str(root))
    try:
        assert get_codex_auth_status()["base_url"] == "https://fixture.invalid/explicit"
    finally:
        reset_secret_scope(scope)
    assert _auth_manifest(root) == before and not events


def test_logged_out_status_leaves_absent_home_absent(observed_config_home, capsys):
    from types import SimpleNamespace
    from hermes_cli.auth_commands import auth_status_command

    home, _ = observed_config_home
    auth_status_command(SimpleNamespace(provider="openai-codex"))
    assert "openai-codex: logged out" in capsys.readouterr().out
    assert not home.exists()

@pytest.mark.parametrize("enabled", [False, True])
def test_logged_out_status_keeps_adoption_notice_without_config_writes(observed_config_home, capsys, enabled):
    from types import SimpleNamespace
    from agent.credential_sources import EXTERNAL_LOGINS_NOT_ADOPTED_NOTICE
    from hermes_cli.auth_commands import auth_status_command

    home, _ = observed_config_home
    home.mkdir()
    (home / "config.yaml").write_text(f"auth:\n  adopt_external_logins: {str(enabled).lower()}\n", encoding="utf-8")
    before = _auth_manifest(home)
    auth_status_command(SimpleNamespace(provider="openai-codex"))
    output = capsys.readouterr().out
    assert "openai-codex: logged out" in output
    assert (EXTERNAL_LOGINS_NOT_ADOPTED_NOTICE in output) is (not enabled)
    unchanged = _auth_manifest(home) == before
    assert unchanged and not (home / "SOUL.md").exists()


def test_corrupt_config_observation_leaves_runtime_backup_enabled(observed_config_home, capsys):
    home, config = observed_config_home
    home.mkdir()
    (home / "config.yaml").write_text("model: [unterminated", encoding="utf-8")
    before = _auth_manifest(home)
    assert isinstance(config.load_config_readonly(observe_only=True), config.FailedConfigRead)
    observed_warning = capsys.readouterr().err
    assert "formatting error" in observed_warning and "A copy" not in observed_warning
    unchanged = _auth_manifest(home) == before
    assert unchanged
    assert isinstance(config.load_config(), config.FailedConfigRead)
    runtime_warning = capsys.readouterr().err
    assert "A copy" in runtime_warning
    assert (home / "SOUL.md").exists()
    assert any((home / "backups" / "config").glob("*.corrupt.*"))


def test_config_observation_reads_existing_good_backup_and_overlay(observed_config_home, monkeypatch):
    home, config = observed_config_home
    home.mkdir()
    cfg = home / "config.yaml"
    cfg.write_text("agent:\n  max_turns: 321\n", encoding="utf-8")
    config.load_config()  # Runtime publishes a good backup in the synthetic home.
    cfg.write_text("model: [unterminated", encoding="utf-8")
    config._LOAD_CONFIG_CACHE.clear()
    config._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    monkeypatch.setattr(config.managed_scope, "load_managed_config", lambda: {"display": {"fixture_overlay": True}})
    before = _auth_manifest(home)
    result = config.load_config_readonly(observe_only=True)
    assert isinstance(result, config.FailedConfigRead)
    assert result["agent"]["max_turns"] == 321 and result["display"]["fixture_overlay"] is True
    unchanged = _auth_manifest(home) == before
    assert unchanged and not config._LOAD_CONFIG_CACHE
