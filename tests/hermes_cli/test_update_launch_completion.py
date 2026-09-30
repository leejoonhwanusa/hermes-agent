"""A fresh launch finishes a source update using PM's success record, not a marker."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import venv_sync
from pm.environments import runtime_facts_path


@pytest.fixture(autouse=True)
def _no_tool_downloads(monkeypatch):
    """Keep launch-sync tests off downloads and the persistent Windows User PATH."""
    import pm.client
    from hermes_cli import _launchers

    path_requests: list[Path] = []

    def register(entry: Path) -> str:
        path_requests.append(entry)
        return "added"

    monkeypatch.setattr(pm.client, "ensure_tools_for_sync", lambda: None)
    monkeypatch.setattr(_launchers, "_register_windows_user_path", register)
    return path_requests


@pytest.fixture
def completion_tail(monkeypatch, _no_tool_downloads):
    """Record the source-completion child prepare_launch spawns after a sync instead of running it.

    The real child is ``hermes_cli/source_completion.py`` from the checkout under test — a
    scratch tree here — building products with the selected interpreter; the tests below
    cover the sync decision, not the build.
    """
    class Spawned(list):
        exit_code = 0
        kwargs: dict = {}
        path_requests: list[Path]

    spawned = Spawned()
    spawned.path_requests = _no_tool_downloads

    def call(command, **kwargs):
        spawned.append(command)
        spawned.kwargs = kwargs
        return spawned.exit_code

    monkeypatch.setattr(venv_sync.subprocess, "call", call)
    return spawned


def _self_checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='example'\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    return root


@pytest.mark.parametrize("argv", [["--version"], ["-V"], ["--help"], ["-p", "work", "-h"]])
def test_metadata_query_never_waits_on_source_completion(tmp_path, monkeypatch, argv):
    """`hermes --version` offline must answer from the tree, not run a network-bound sync."""
    import pm

    root = _self_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: pytest.fail("metadata query reached PM"))
    assert venv_sync.prepare_launch(root, argv) is None


def test_failed_completion_tail_is_retried_without_rebuilding_dependencies(tmp_path, monkeypatch, completion_tail):
    """Dependencies committed, tail failed: the next launch owes the tail only."""
    import pm
    from hermes_cli import _launchers

    root = _self_checkout(tmp_path, monkeypatch)
    fact = runtime_facts_path(root)
    syncs = []
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: fact.is_file())
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))

    def sync(extras=None, **kwargs):
        syncs.append(extras)
        fact.parent.mkdir(parents=True, exist_ok=True)
        fact.write_text(json.dumps({"packages": {"venv": {"stamp": "complete", "extras": ["all"]}}}))

    monkeypatch.setattr(pm, "sync_venv", sync)
    completion_tail.exit_code = 1
    with pytest.raises(RuntimeError, match="run `hermes update`"):
        venv_sync.prepare_launch(root, [])
    assert len(syncs) == 1 and len(completion_tail) == 1
    assert pm.venv_is_current()

    with pytest.raises(RuntimeError, match="run `hermes update`"):
        venv_sync.prepare_launch(root, [])
    assert len(syncs) == 1, "current dependencies were rebuilt for a tail retry"
    assert len(completion_tail) == 2

    completion_tail.exit_code = 0
    # Dependencies are already this interpreter's: the tail alone owes no re-exec.
    assert venv_sync.prepare_launch(root, []) is None
    assert len(syncs) == 1 and len(completion_tail) == 3
    assert venv_sync.prepare_launch(root, []) is None
    assert len(completion_tail) == 3, "a finished tail was run again"


def test_completion_tail_output_stays_off_stdout(tmp_path, monkeypatch, completion_tail):
    """The automatic tail runs in front of the user's command, which may be piping JSON."""
    import pm
    from hermes_cli import _launchers

    root = _self_checkout(tmp_path, monkeypatch)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: None)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))
    venv_sync.prepare_launch(root, [])
    assert completion_tail.kwargs["stdout"] is sys.__stderr__
    if sys.platform == "win32":
        assert completion_tail.path_requests == [tmp_path / "home" / "bin"]


def test_first_launch_syncs_without_marker_then_uses_completion_fact(tmp_path, monkeypatch, completion_tail):
    import pm
    from hermes_cli import _launchers

    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='example'\n")
    (root / "uv.lock").write_text("lock\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    fact = runtime_facts_path(root)
    calls = []
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: fact.is_file())
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))

    def sync(extras=None, **kwargs):
        calls.append((extras, kwargs))
        fact.parent.mkdir(parents=True, exist_ok=True)
        fact.write_text(json.dumps({"packages": {"venv": {"stamp": "complete", "extras": ["all"]}}}))

    monkeypatch.setattr(pm, "sync_venv", sync)
    # A shipped updater may have written this before it reaches an inert shim.
    # It is obsolete after successful sync, not the trigger for that sync.
    (root / ".update-incomplete").write_text("pid=-1\n")
    assert venv_sync.prepare_launch(root, []) == Path(sys.executable)
    assert calls == [(["all"], {"explicit": True, "project_root": root, "evict_incompatible_plugins": True})]
    assert not (root / ".update-incomplete").exists()
    if sys.platform == "win32":
        assert completion_tail.path_requests == [tmp_path / "home" / "bin"]
    assert any("source_completion.py" in str(part) for cmd in completion_tail for part in cmd)
    assert venv_sync.prepare_launch(root, []) is None
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["script", "module", "command"])
def test_relaunch_keeps_invocation_and_checkout_imports(tmp_path, mode):
    root = tmp_path / "source"
    root.mkdir()
    (root / "checkout_only.py").write_text("value = 'from checkout'\n")
    script = root / "entry.py"
    script.write_text("import checkout_only, json, sys\nprint(json.dumps([checkout_only.value, sys.argv[1:]]))\n")
    argv = [str(script), "--profile", "name with spaces", "-c", "session"]
    orig = [sys.executable, *argv]
    module = None
    if mode == "module":
        module = "entry"
        orig = [sys.executable, "-m", module, *argv[1:]]
    elif mode == "command":
        argv[0] = "-c"
        orig = [sys.executable, "-c", "import entry", *argv[1:]]
    command = venv_sync.relaunch_command(Path(sys.executable), root, argv, orig, module)
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["from checkout", argv[1:]]


@pytest.mark.parametrize("owner,argv", [(None, []), ("external", []), ("electron-updater", []), ("self", ["-p", "coder", "pm", "repair"])])
def test_non_self_or_pm_launch_cannot_trigger_update(tmp_path, monkeypatch, owner, argv):
    import pm
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    if owner:
        (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": owner}))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: pytest.fail("unowned launch reached PM"))
    assert venv_sync.prepare_launch(root, argv) is None


def test_failed_launch_keeps_previous_completion_and_retries(tmp_path, monkeypatch):
    import pm
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    fact = runtime_facts_path(root)
    fact.parent.mkdir(parents=True)
    previous = '{"packages":{"venv":{"stamp":"previous","extras":["all","anthropic"]}}}'
    fact.write_text(previous)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    calls = []
    def fail(extras, **kwargs):
        calls.append(extras)
        raise RuntimeError("network unavailable")
    monkeypatch.setattr(pm, "sync_venv", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="network unavailable"):
            venv_sync.prepare_launch(root, [])
        assert fact.read_text() == previous
    assert calls == [None, None]
    assert not (root / ".update-incomplete").exists()


def test_blessed_legacy_install_is_adopted_before_sync(tmp_path, monkeypatch, completion_tail):
    import pm
    from hermes_cli import _launchers
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    root = home / "hermes-agent"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    calls = []
    monkeypatch.setattr(pm, "sync_venv", lambda *args, **kw: calls.append(args))
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))
    assert venv_sync.prepare_launch(root, []) == Path(sys.executable)
    assert json.loads((root / "install-stamp.json").read_text())["source"] == "adoption"
    assert calls == [(["all"],)]
    if sys.platform == "win32":
        assert completion_tail.path_requests == [home / "bin"]


def test_relaunch_runs_zip_launchers_and_preserves_interpreter_options(tmp_path):
    import zipfile
    launcher = tmp_path / "hermes.exe"
    with zipfile.ZipFile(launcher, "w") as archive:
        archive.writestr("__main__.py", "import json,sys; print(json.dumps([sys.argv[1:], sys.stdout.write_through, sys.flags.utf8_mode]))")
    original = [sys.executable, "-u", "-X", "utf8", str(launcher), "arg with spaces"]
    command = venv_sync.relaunch_command(Path(sys.executable), tmp_path, [str(launcher), "arg with spaces"], original, "__main__")
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [["arg with spaces"], True, 1]


def test_live_old_update_blocks_launch_sync(tmp_path, monkeypatch):
    import pm
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    marker = root / ".update-incomplete"
    marker.write_text(f"pid={os.getpid()}\n")
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: pytest.fail("raced old updater"))
    with pytest.raises(RuntimeError, match="still running"):
        venv_sync.prepare_launch(root, [])
    assert marker.is_file()
    # Fresh post-sync verification children may boot under a live updater.
    from hermes_cli import _launchers
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: Path(sys.executable))
    assert venv_sync.prepare_launch(root, []) is None
    assert marker.is_file()


def test_launch_under_the_owning_update_does_not_run_the_tail_again(tmp_path, monkeypatch, completion_tail):
    """The tail imports the application, whose entry point runs prepare_launch: inside the
    process tree of the update that owns the pending tail it must be a no-op, not recurse."""
    import time
    import pm
    from hermes_cli.update_lock import update_marker_path

    root = _self_checkout(tmp_path, monkeypatch)
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True)
    pending.write_text("owed\n")
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    marker = update_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{os.getppid()}\n{int(time.time())}\n")  # an ancestor holds the update

    assert venv_sync.prepare_launch(root, []) is None
    assert completion_tail == []
    assert pending.is_file(), "the owning update's obligation was discharged by its own tail"


def test_long_source_completion_does_not_start_another_tail(tmp_path, monkeypatch, completion_tail):
    """A long product build can outlive the update marker's 20-minute ceiling."""
    import time
    import pm
    from hermes_cli.source_completion import complete_source_checkout
    from hermes_cli.update_lock import UPDATE_MARKER_MAX_AGE_SECONDS, update_marker_path

    root = _self_checkout(tmp_path, monkeypatch)
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True)
    pending.write_text("owed\n")
    marker = update_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{os.getpid()}\n{time.time() - UPDATE_MARKER_MAX_AGE_SECONDS - 1}\n")
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: True)
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda _root: None)
    monkeypatch.setattr("hermes_cli._launchers.resolve_store_python", lambda _root: Path(sys.executable))

    def build(_root, *, desktop):
        assert venv_sync.prepare_launch(root, []) is None

    monkeypatch.setattr("hermes_cli.source_build.build_update_products", build)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._run_post_update_maintenance", lambda **kw: False)

    assert not complete_source_checkout(root, desktop=False, assume_yes=True)
    assert completion_tail == []
    assert pending.is_file()

    # The real CLI bootstrap catches preparation errors. Its swallowed drift
    # must still make the outer completion fail and leave the obligation due.
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: False)

    def build_with_swallowed_drift(_root, *, desktop):
        with pytest.raises(RuntimeError, match="dependencies changed"):
            venv_sync.prepare_launch(root, [])

    monkeypatch.setattr("hermes_cli.source_build.build_update_products", build_with_swallowed_drift)
    monkeypatch.setattr("hermes_cli.update_cmd_maint._run_post_update_maintenance", lambda **kw: True)
    monkeypatch.setattr("hermes_cli.source_stamp.write_source_stamp", lambda _root: None)
    with pytest.raises(RuntimeError, match="dependencies changed"):
        complete_source_checkout(root, desktop=False, assume_yes=True)
    assert pending.is_file()

@pytest.mark.parametrize("case", [
    "ready", "relaunch", "stale", "pending", "missing", "corrupt", "error",
    "worker-success", "worker-error", "worker-corrupt", "worker-no-response",
    "worker-late-response", "worker-default",
    "worker-prepare-lock", "worker-validation-timeout", "worker-validation-error",
    "worker-prepare-lease", "worker-spawn-delay", "worker-cleanup-delay",
    "worker-chain-ready", "worker-activation-lease", "worker-validation-spawn-delay",
    "activation-ready", "activation-lock", "activation-journal-race",
    "activation-facts-race", "activation-lease-race", "activation-input-race", "activation-default",
])
def test_auth_status_preparation_policy(tmp_path, monkeypatch, completion_tail, case, record_property):
    """Exact status never starts maintenance; its canonical probe owns a finite budget."""
    import io
    import pm
    import pm.client
    import pm.receipt
    import pm.registry
    from hermes_cli import _launchers
    from pm.environments import install_state_dir, site_packages

    root = _self_checkout(tmp_path, monkeypatch)
    if case.startswith("activation-"):
        from contextlib import contextmanager
        import site
        import pm.environments as environments
        from hermes_cli import runtime_state

        state = install_state_dir(root)
        environment = state / "environments" / "fixture" / "venv"
        environment.mkdir(parents=True)
        (environment / "pyvenv.cfg").write_text("version = 3.14.7\n")
        selected = site_packages(environment)
        selected.mkdir(parents=True)
        facts = runtime_facts_path(root)
        facts.write_text(json.dumps({"packages": {"venv": {"environment": str(environment)}}}))
        snapshot = facts.read_bytes()
        inputs = environments.activation_input_mtimes(root)
        events = []
        @contextmanager
        def lock(project, **kwargs):
            assert project == root
            assert kwargs == ({} if case == "activation-default" else {"timeout": 0})
            events.append("lock")
            if case == "activation-journal-race":
                (state / "publication.json").write_text("{}")
            elif case == "activation-facts-race":
                facts.write_bytes(snapshot + b" ")
            yield case != "activation-lock"
        def recover(project):
            assert case == "activation-default"
            events.append("recover")
        def lease(project):
            assert project == environment
            events.append("lease")
            if case == "activation-lease-race":
                facts.write_bytes(snapshot + b" ")
            elif case == "activation-input-race":
                (root / "pyproject.toml").write_text("[project]\nname='changed'\n")
            return lambda: events.append("release")
        monkeypatch.setattr(runtime_state, "runtime_lock", lock)
        monkeypatch.setattr(runtime_state, "recover_publication", recover)
        monkeypatch.setattr(runtime_state, "lease_generation", lease)
        monkeypatch.setattr(site, "addsitedir", lambda path: events.append("site"))
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.delenv("PYTHONPATH", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        if case == "activation-default":
            environments.activate_dependencies(root)
            assert events == ["lock", "recover", "lease", "site"]
        elif case == "activation-ready":
            environments.activate_dependencies(root, read_only=True,
                                              expected_facts=snapshot, expected_inputs=inputs)
            assert events == ["lock", "lease", "site"]
            assert str(selected) in sys.path
        else:
            with pytest.raises(RuntimeError, match="runtime-not-ready"):
                environments.activate_dependencies(root, read_only=True,
                                                  expected_facts=snapshot, expected_inputs=inputs)
            assert "recover" not in events and "site" not in events
            assert events == (["lock", "lease", "release"] if case in {
                "activation-lease-race", "activation-input-race"} else ["lock"])
        return
    if case.startswith("worker-"):
        client = pm.client
        import time
        from pm import paths, runtime
        from pm.lock import Lockfile
        from pm.registry import get_package
        from pm.store import current_target
        from pm.environments import activate_dependencies, activation_input_mtimes, store_root, venv_python
        from pm.install import venv_is_current as canonical_currency
        from pm.packages import Venv
        from hermes_cli import runtime_state

        # Real resolver/prepare chain; only leaf process/clock/kernel locks are simulated.
        # Existing home override selects exclusively temporary PM/application metadata.
        tools = store_root(root)
        tools.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tools))
        pin = Lockfile(paths.lockfile_path())
        target = current_target()
        tool_facts = {}
        for name in ("uv", "python"):
            package = get_package(name)
            entry = tools / name
            binary = package.binary(entry, target)
            assert binary is not None
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.touch(mode=0o755)
            tool_facts[name] = {"entry": name, "version": pin.version(name), "target": target,
                                "artifacts": [a["sha256"] for a in pin.artifacts(name, target)]}
        (tools / "facts.json").write_text(json.dumps({"schema": 1, "packages": tool_facts}))
        python = get_package("python").binary(tools / "python", target)
        assert python is not None
        pm_state = install_state_dir(paths.repo_root()) / "pm-runtime"
        pm_generation = pm_state / "generations" / "fixture"
        pm_generation.mkdir(parents=True)
        (pm_generation / "pm-runtime.json").write_text("{}")
        (pm_generation / ".lease-managed").touch()
        pm_python = venv_python(pm_generation)
        pm_python.parent.mkdir(parents=True, exist_ok=True)
        pm_python.touch()
        identity = runtime._inputs(Path(runtime.__file__).parent, python)
        (pm_state / "selected.json").write_text(json.dumps({"inputs": identity, "generation": "generations/fixture"}))
        monkeypatch.setattr(runtime, "_HELD", {})
        app = install_state_dir(root) / "environments" / "fixture" / "venv"
        app.mkdir(parents=True)
        (app / "pyvenv.cfg").write_text("version = 3.14.7\n")
        (app.parent / ".lease-managed").touch()
        site_packages(app).mkdir(parents=True)
        (root / "uv.lock").write_text("version = 1\n")
        (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text("plugins:\n  enabled: []\nmemory:\n  provider: none\n")
        stamp = Venv(root).expected_stamp([])
        facts = runtime_facts_path(root)
        facts.write_text(json.dumps({"schema": 1, "packages": {"venv": {
            "environment": str(app), "stamp": stamp, "extras": []}}}))
        snapshot, inputs = facts.read_bytes(), activation_input_mtimes(root)
        now = [0.0]
        events = []
        class Wire(io.StringIO):
            def readline(self, size: int = -1, /) -> str:
                return process.readline()
        phases = {"validated": False, "worker": False, "activation_lock": False}
        def os_lock(fd, mode, size=None):
            if os.name == "nt":
                import msvcrt
                if mode == msvcrt.LK_UNLCK:
                    return
            if phases["worker"] and not phases["activation_lock"]:
                phases["activation_lock"] = True
                events.append(("lock", "activation-publication"))
                return
            phase = "activation-lease" if phases["worker"] else ("prepare-lease" if phases["validated"] else "prepare-lock")
            if case == "worker-" + phase:
                import errno
                raise BlockingIOError(errno.EACCES, "fixture lock contention")
            events.append(("lock", phase))
        if os.name == "nt":
            import msvcrt
            monkeypatch.setattr(msvcrt, "locking", os_lock)
        else:
            import fcntl
            monkeypatch.setattr(fcntl, "flock", os_lock)
        monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
        class Validation:
            returncode = None
            transport: io.TextIOWrapper
            def poll(self): return self.returncode
            def kill(self):
                events.append(("validation-kill", None))
                self.returncode = -9
            def wait(self, timeout=None):
                events.append(("validation-wait", timeout))
                if self.returncode == -9:
                    now[0] += 0.25
                    return -9
                assert timeout is not None
                if case == "worker-validation-timeout":
                    now[0] += timeout
                    raise subprocess.TimeoutExpired("fixture validation", timeout)
                now[0] += 0.5
                self.returncode = 1 if case == "worker-validation-error" else 0
                phases["validated"] = True
                return self.returncode
        validation = Validation()
        class Process:
            returncode = None
            def __init__(self):
                self.stdin = io.StringIO()
                self.stdout = Wire()
            def response(self, message):
                if case == "worker-corrupt":
                    return "invalid JSON"
                result = {"id": message["id"], "type": "result", "result": canonical_currency(project_root=root)}
                if case == "worker-error":
                    result["error"] = {"type": "ValueError", "message": "fixture corrupt facts"}
                return json.dumps(result) + "\n"
            def readline(self):
                events.append(("readline", None))
                return self.response(json.loads(self.stdin.getvalue()))
            def poll(self):
                return self.returncode
            def kill(self):
                events.append(("kill", None))
                self.returncode = -9
            def wait(self, timeout: float | None = None):
                events.append(("wait", timeout))
                if case == "worker-default":
                    self.returncode = 0
                    return 0
                assert timeout is not None
                if self.returncode == -9:
                    delay = 0.9 if case == "worker-cleanup-delay" else 0.25
                    assert delay <= timeout
                    now[0] += delay
                    return -9
                self.message = json.load(self.stdin)
                assert self.message["operation"] == "venv_is_current"
                if case in {"worker-no-response", "worker-cleanup-delay"}:
                    now[0] += timeout
                    raise subprocess.TimeoutExpired("fixture worker", timeout)
                if case == "worker-late-response":
                    now[0] += timeout + 0.1
                self.stdout.write(self.response(self.message))
                self.stdout.flush()
                self.returncode = 0
                return 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stdin.close()
                self.stdout.close()

        process = Process()
        monkeypatch.setattr(client.time, "monotonic", lambda: now[0])
        def popen(command, **kwargs):
            if "-c" in command and "import packaging" in command[command.index("-c") + 1]:
                events.append(("validation-spawn", None))
                if case == "worker-validation-spawn-delay":
                    now[0] += 9
                validation.transport = kwargs["stderr"]
                return validation
            assert Path(command[0]) == pm_python
            phases["worker"] = True
            events.append(("worker-spawn", None))
            if case == "worker-spawn-delay":
                now[0] += 9
            if case != "worker-default":
                process.stdin, process.stdout = kwargs["stdin"], kwargs["stdout"]
            return process
        monkeypatch.setattr(client.subprocess, "Popen", popen)
        def run(command, **kwargs):
            assert case == "worker-default" and kwargs["timeout"] == 30
            phases["validated"] = True
            events.append(("default-validation", 30))
            return subprocess.CompletedProcess(command, 0, "", "")
        monkeypatch.setattr(runtime.subprocess, "run", run)
        try:
            if case == "worker-default":
                assert client.venv_is_current(project_root=root)
                assert ("readline", None) in events and ("wait", 5) in events
                assert ("validation-wait", 30) not in events  # Default validation uses subprocess.run below.
            else:
                error = {"worker-error": ValueError, "worker-corrupt": ValueError,
                         "worker-no-response": TimeoutError, "worker-late-response": TimeoutError,
                         "worker-prepare-lock": TimeoutError, "worker-validation-timeout": TimeoutError,
                         "worker-prepare-lease": TimeoutError, "worker-spawn-delay": TimeoutError,
                         "worker-cleanup-delay": TimeoutError, "worker-validation-spawn-delay": TimeoutError}.get(case)
                if error:
                    with pytest.raises(error):
                        client.venv_is_current(project_root=root, deadline=10)
                elif case == "worker-validation-error":
                    from pm.package import InstallError
                    with pytest.raises(InstallError, match="pm-runtime"):
                        client.venv_is_current(project_root=root, deadline=10)
                else:
                    if case == "worker-chain-ready":
                        assert venv_sync.prepare_launch(root, ["auth", "status", "openai-codex"], deadline=10) == python
                    else:
                        assert client.venv_is_current(project_root=root, deadline=10)
                    if case in {"worker-chain-ready", "worker-activation-lease"}:
                        before = list(sys.path)
                        monkeypatch.setattr(sys, "path", before)
                        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
                        if case == "worker-activation-lease":
                            with pytest.raises(TimeoutError):
                                activate_dependencies(root, read_only=True, expected_facts=snapshot,
                                                      expected_inputs=inputs, deadline=10)
                        else:
                            activate_dependencies(root, read_only=True, expected_facts=snapshot,
                                                  expected_inputs=inputs, deadline=10)
                            assert str(site_packages(app)) in sys.path
                assert now[0] <= 10.05, events  # Existing lock polling can overshoot by one 50ms tick.
                assert ("readline", None) not in events and ("wait", 5) not in events
                if case in {"worker-prepare-lock", "worker-validation-timeout", "worker-prepare-lease", "worker-validation-error"}:
                    assert not phases["worker"]
                if case == "worker-prepare-lock":
                    assert not phases["validated"]
                if case in {"worker-no-response", "worker-cleanup-delay"}:
                    waits = [timeout for event, timeout in events if event == "wait"]
                    assert waits == [8.5, 1]
                if phases["worker"]:
                    assert process.stdin.closed and process.stdout.closed
                    for stream in (process.stdin, process.stdout):
                        if isinstance(stream.name, str):
                            assert not Path(stream.name).exists()
                    assert process.poll() is not None
                if validation.returncode is not None:
                    assert validation.poll() is not None and validation.transport.closed
                    if isinstance(validation.transport.name, str):
                        assert not Path(validation.transport.name).exists()
        finally:
            record_property("virtual_elapsed", now[0])
            record_property("deadline_trace", json.dumps(events))
            for release in runtime._HELD.values():
                release()
        return

    fact = runtime_facts_path(root)
    environment = install_state_dir(root) / "environments" / "fixture" / "venv"
    environment.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("version = 3.14.7\n")
    site_packages(environment).mkdir(parents=True)
    fact.parent.mkdir(parents=True, exist_ok=True)
    fact.write_text(json.dumps({"packages": {"venv": {
        "stamp": "fixture", "extras": [], "environment": str(environment),
    }}}))
    if case == "pending":
        venv_sync.completion_pending_path(root).write_text("fixture pending")
    elif case == "missing":
        fact.unlink()
    elif case == "corrupt":
        fact.write_text("invalid JSON")
    calls = []
    def current(**kwargs):
        assert kwargs["project_root"] == root
        assert kwargs["deadline"] > 0 and set(kwargs) == {"project_root", "deadline"}
        calls.append(kwargs)
        if case == "error":
            raise TimeoutError("fixture deadline")
        return case != "stale"
    monkeypatch.setattr(pm, "venv_is_current", current)
    python = root / "managed-python" if case == "relaunch" else Path(sys.executable)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: python)
    monkeypatch.setattr(pm, "sync_venv", lambda *a, **kw: pytest.fail("status started sync"))
    monkeypatch.setattr(venv_sync, "_finish_source_update", lambda *a, **kw: pytest.fail("status started completion"))
    if case in {"ready", "relaunch"}:
        assert venv_sync.prepare_launch(root, ["auth", "status", "openai-codex"]) == (
            python if case == "relaunch" else None
        )
    else:
        with pytest.raises(venv_sync.RuntimeNotReady, match="runtime-not-ready"):
            venv_sync.prepare_launch(root, ["auth", "status", "openai-codex"])
    if case == "relaunch":
        argv = ["-c", "auth", "status", "openai-codex"]
        command = venv_sync.relaunch_command(python, root, argv, [str(python), "-c", "pass"], None, deadline=123.5)
        monkeypatch.setattr(sys, "argv", list(sys.argv))
        monkeypatch.setattr(sys, "path", list(sys.path))
        globals_dict = {}
        exec(command[-1], globals_dict)
        assert getattr(sys, "_hermes_status_deadline") == 123.5
        delattr(sys, "_hermes_status_deadline")
    assert len(calls) == (1 if case in {"ready", "relaunch", "stale", "error"} else 0)
    assert not completion_tail and not completion_tail.path_requests
