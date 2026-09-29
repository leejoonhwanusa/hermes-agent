"""Tests for the BasicAuthProvider plugin (username/password, scrypt, signed
tokens).

Loads the plugin module directly (it's a bundled backend plugin, not on the
import path as a package) and exercises the provider behaviour + the
``register(ctx)`` entry point's config/env resolution and skip reasons.
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock

import pytest

import plugins.dashboard_auth.basic as basic_plugin
from hermes_cli.dashboard_auth import (
    InvalidCredentialsError,
    RefreshExpiredError,
    assert_protocol_compliance,
)


@pytest.fixture(scope="module")
def basic():
    return basic_plugin


@pytest.fixture(autouse=True)
def _clear_basic_env(monkeypatch):
    for var in (
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
        "HERMES_DASHBOARD_BASIC_AUTH_TTL_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_hash_then_verify_round_trips(self, basic):
        h = basic.hash_password("hunter2")
        assert h.startswith("scrypt$")
        assert basic._verify_password("hunter2", h)

    def test_wrong_password_fails(self, basic):
        h = basic.hash_password("hunter2")
        assert not basic._verify_password("wrong", h)

    def test_malformed_hash_returns_false(self, basic):
        assert not basic._verify_password("x", "not-a-valid-hash")
        assert not basic._verify_password("x", "bcrypt$wrong$scheme")

    def test_two_hashes_of_same_password_differ(self, basic):
        # Distinct random salts → distinct encoded hashes.
        assert basic.hash_password("pw") != basic.hash_password("pw")


# ---------------------------------------------------------------------------
# Provider behaviour
# ---------------------------------------------------------------------------


class TestProvider:
    def _make(self, basic, **kw):
        h = basic.hash_password("hunter2")
        return basic.BasicAuthProvider(
            username="admin",
            password_hash=h,
            secret=secrets.token_bytes(32),
            **kw,
        )

    def test_protocol_compliant(self, basic):
        assert assert_protocol_compliance(basic.BasicAuthProvider) is None


    def test_login_mints_session(self, basic):
        p = self._make(basic)
        s = p.complete_password_login(username="admin", password="hunter2")
        assert s.user_id == "admin"
        assert s.provider == "basic"
        assert s.access_token and s.refresh_token

    def test_bad_credentials_raise(self, basic):
        p = self._make(basic)
        for u, pw in [("admin", "wrong"), ("ghost", "hunter2"), ("", "")]:
            with pytest.raises(InvalidCredentialsError):
                p.complete_password_login(username=u, password=pw)

    def test_verify_round_trips_and_rejects_tamper(self, basic):
        p = self._make(basic)
        s = p.complete_password_login(username="admin", password="hunter2")
        assert p.verify_session(access_token=s.access_token) is not None
        assert p.verify_session(access_token="garbage") is None

    def test_access_token_not_accepted_as_refresh(self, basic):
        p = self._make(basic)
        s = p.complete_password_login(username="admin", password="hunter2")
        # A refresh token must not verify as an access token and vice
        # versa — the ``kind`` claim is enforced.
        assert p.verify_session(access_token=s.refresh_token) is None
        with pytest.raises(RefreshExpiredError):
            p.refresh_session(refresh_token=s.access_token)

    def test_refresh_round_trips(self, basic):
        p = self._make(basic)
        s = p.complete_password_login(username="admin", password="hunter2")
        r = p.refresh_session(refresh_token=s.refresh_token)
        assert r.user_id == "admin"
        assert p.verify_session(access_token=r.access_token) is not None


    def test_cross_secret_token_does_not_verify(self, basic):
        p1 = self._make(basic)
        p2 = self._make(basic)  # different random secret
        s = p1.complete_password_login(username="admin", password="hunter2")
        assert p2.verify_session(access_token=s.access_token) is None


    @pytest.mark.parametrize('access_only', [False, True])
    def test_logout_persists_and_preserves_other_login(self, basic, access_only):
        p = self._make(basic)
        first = p.complete_password_login(username='admin', password='hunter2')
        other = p.complete_password_login(username='admin', password='hunter2')
        rotated = p.refresh_session(refresh_token=first.refresh_token)
        p.logout_session(access_token=first.access_token,
                         refresh_token='' if access_only else first.refresh_token)
        restarted = basic.BasicAuthProvider(username='admin', password_hash=p._password_hash,
                                            secret=p._secret)
        for session in (first, rotated):
            assert restarted.verify_session(access_token=session.access_token) is None
            with pytest.raises(RefreshExpiredError):
                restarted.refresh_session(refresh_token=session.refresh_token)
        assert restarted.verify_session(access_token=other.access_token) is not None
        stored = p._sessions_path.read_text()
        assert first.access_token not in stored and other.refresh_token not in stored

    def test_profiles_a_b_a_are_isolated(self, basic, tmp_path):
        from agent.secret_scope import is_multiplex_active, set_multiplex_active
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        previous = is_multiplex_active()
        set_multiplex_active(True)
        tokens = [set_hermes_home_override(tmp_path / 'a')]
        try:
            a = self._make(basic)
            session = a.complete_password_login(username='admin', password='hunter2')
            tokens.append(set_hermes_home_override(tmp_path / 'b'))
            b = basic.BasicAuthProvider(username='admin', password_hash=a._password_hash, secret=a._secret)
            assert b.verify_session(access_token=session.access_token) is None
            # An already-created provider stays attached to its original profile.
            assert a.verify_session(access_token=session.access_token) is not None
            tokens.append(set_hermes_home_override(tmp_path / 'a'))
            again = basic.BasicAuthProvider(username='admin', password_hash=a._password_hash, secret=a._secret)
            assert again.verify_session(access_token=session.access_token) is not None
        finally:
            for token in reversed(tokens):
                reset_hermes_home_override(token)
            set_multiplex_active(previous)

    def test_legacy_missing_and_corrupt_store_fail_closed(self, basic):
        import time
        from hermes_cli.dashboard_auth import ProviderError

        p = self._make(basic)
        session = p.complete_password_login(username='admin', password='hunter2')
        legacy = basic._sign({'sub': 'admin', 'kind': 'access', 'exp': int(time.time()) + 3600}, p._secret)
        assert p.verify_session(access_token=legacy) is None
        p._sessions_path.unlink()
        assert p.verify_session(access_token=session.access_token) is None
        with pytest.raises(RefreshExpiredError):
            p.refresh_session(refresh_token=session.refresh_token)
        p._sessions_path.write_text('broken', encoding='utf-8')
        with pytest.raises(ProviderError):
            p.verify_session(access_token=session.access_token)
        with pytest.raises(ProviderError):
            p.complete_password_login(username='admin', password='hunter2')
        with pytest.raises(ProviderError):
            p.logout_session(access_token=session.access_token, refresh_token='')
        assert p._sessions_path.read_text() == 'broken'

    def test_revocation_seen_by_another_process(self, basic):
        import json
        import subprocess
        import sys

        p = self._make(basic)
        session = p.complete_password_login(username='admin', password='hunter2')
        child = '''
import json, sys
from plugins.dashboard_auth.basic import BasicAuthProvider
d = json.load(sys.stdin)
p = BasicAuthProvider(username='admin', password_hash=d['hash'], secret=bytes.fromhex(d['secret']))
assert p.verify_session(access_token=d['token']) is not None
p.logout_session(access_token=d['token'], refresh_token='')
'''
        result = subprocess.run([sys.executable, '-c', child], input=json.dumps({
            'hash': p._password_hash, 'secret': p._secret.hex(), 'token': session.access_token}),
            text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert p.verify_session(access_token=session.access_token) is None
        with pytest.raises(RefreshExpiredError):
            p.refresh_session(refresh_token=session.refresh_token)

    @pytest.mark.parametrize('first', ['refresh', 'logout'])
    def test_refresh_logout_serialization(self, basic, monkeypatch, first):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        p = self._make(basic)
        session = p.complete_password_login(username='admin', password='hunter2')
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
        original_save = p._save_sessions

        def save(data):
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            original_save(data)

        monkeypatch.setattr(p, '_save_sessions', save)

        def operation(kind, second=False):
            if second:
                second_started.set()
            if kind == 'logout':
                return p.logout_session(access_token=session.access_token, refresh_token='')
            try:
                return p.refresh_session(refresh_token=session.refresh_token)
            except RefreshExpiredError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            leading = pool.submit(operation, first)
            try:
                assert entered.wait(5)
                following = pool.submit(operation, 'logout' if first == 'refresh' else 'refresh', True)
                assert second_started.wait(5)
            finally:
                release.set()
            results = (leading.result(timeout=10), following.result(timeout=10))
        for result in results:
            if result is not None:
                assert p.verify_session(access_token=result.access_token) is None
        assert p.verify_session(access_token=session.access_token) is None
        with pytest.raises(RefreshExpiredError):
            p.refresh_session(refresh_token=session.refresh_token)

    def test_oauth_methods_raise_not_implemented(self, basic):
        p = self._make(basic)
        with pytest.raises(NotImplementedError):
            p.start_login(redirect_uri="https://x/auth/callback")
        with pytest.raises(NotImplementedError):
            p.complete_login(
                code="c", state="s", code_verifier="v", redirect_uri="r"
            )

    def test_construction_validates_inputs(self, basic):
        good_hash = basic.hash_password("pw")
        with pytest.raises(ValueError):
            basic.BasicAuthProvider(
                username="", password_hash=good_hash, secret=b"x" * 32
            )
        with pytest.raises(ValueError):
            basic.BasicAuthProvider(
                username="admin", password_hash="", secret=b"x" * 32
            )
        with pytest.raises(ValueError):
            basic.BasicAuthProvider(
                username="admin", password_hash=good_hash, secret=b"short"
            )


# ---------------------------------------------------------------------------
# register() entry point — config/env resolution + skip reasons
# ---------------------------------------------------------------------------


class TestRegister:
    def test_skips_when_no_username(self, basic, monkeypatch):
        monkeypatch.setattr(basic, "_load_config_basic_auth_section", lambda: {})
        ctx = MagicMock()
        basic.register(ctx)
        ctx.register_dashboard_auth_provider.assert_not_called()
        assert "username" in basic.LAST_SKIP_REASON


    def test_registers_with_env_plaintext_password(self, basic, monkeypatch):
        monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "admin")
        monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "hunter2")
        monkeypatch.setattr(basic, "_load_config_basic_auth_section", lambda: {})
        ctx = MagicMock()
        basic.register(ctx)
        ctx.register_dashboard_auth_provider.assert_called_once()
        provider = ctx.register_dashboard_auth_provider.call_args.args[0]
        assert isinstance(provider, basic.BasicAuthProvider)
        # Round-trips: the registered provider authenticates the env creds.
        s = provider.complete_password_login(username="admin", password="hunter2")
        assert s.user_id == "admin"
        assert basic.LAST_SKIP_REASON == ""


    def test_env_password_overrides_config(self, basic, monkeypatch):
        cfg_hash = basic.hash_password("config-pw")
        monkeypatch.setattr(
            basic,
            "_load_config_basic_auth_section",
            lambda: {"username": "admin", "password_hash": cfg_hash},
        )
        # Env plaintext should win over the config hash.
        monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "env-pw")
        ctx = MagicMock()
        basic.register(ctx)
        provider = ctx.register_dashboard_auth_provider.call_args.args[0]
        # env password works ...
        assert provider.complete_password_login(
            username="admin", password="env-pw"
        )
        # ... and the config password no longer does.
        with pytest.raises(InvalidCredentialsError):
            provider.complete_password_login(username="admin", password="config-pw")

    def test_explicit_secret_makes_sessions_portable(self, basic, monkeypatch):
        # Two providers built from the SAME explicit secret accept each
        # other's tokens (the restart-/multi-worker-survival contract).
        shared = secrets.token_bytes(32).hex()
        monkeypatch.setattr(basic, "_load_config_basic_auth_section", lambda: {})
        monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "admin")
        monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "hunter2")
        monkeypatch.setenv("HERMES_DASHBOARD_BASIC_AUTH_SECRET", shared)

        ctx1, ctx2 = MagicMock(), MagicMock()
        basic.register(ctx1)
        basic.register(ctx2)
        p1 = ctx1.register_dashboard_auth_provider.call_args.args[0]
        p2 = ctx2.register_dashboard_auth_provider.call_args.args[0]
        s = p1.complete_password_login(username="admin", password="hunter2")
        assert p2.verify_session(access_token=s.access_token) is not None
