# Source update completion ownership

## Phase seam

The command process owns admission, the update lock and output lifetime, pre-update
inventory, all-profile snapshots, gateway pause, Git selection/stash/restore and
syntax/HEAD guards, and the ZIP download/stage/dirty recheck/release graft/swap.
It imports the completion transport before swapping code. Once the final tree is
selected (including upstream merge), Git, already-current retry and ZIP all send
one versioned JSON request to `update_completion.py` **from that tree**. No cached
application module is evicted or reloaded in the command process.

The request carries canonical source/home, desktop product selection, interactive
and gateway mode, pre-update version, active and sibling snapshot identifiers,
serialized runtime plan, open receipt identity/data and paused-Windows token. It
contains data, never callables or pickles. stdin stays inherited for interactive
configuration prompts; gateway mode retains its non-interactive behavior. Child
output stays visible and is mirrored by the parent's update output stream.

## New-code owner

A stdlib-only entrypoint starts using the available Python with `-I -S`, so no
old site-packages or executable `.pth` files initialize. A private bytecode-cache
prefix fences stale cache files before any new-checkout imports. Its explicit
import path points at the new checkout. It calls the new PM interface to prepare the
recorded dependency union, then starts the selected Python with the new activation
environment. That interpreter also starts with site initialization disabled,
then the runtime owner leases and activates its selected generation before any
application imports. Only that interpreter imports application completion code. The same
receipt/correlation identity crosses this preparation boundary (including PM
results). Selected-Python completion owns launcher publication, builders, cache
invalidation, all-profile configuration/state/skills maintenance, process scans,
fleet restart, Windows resume, dashboard deduplication and verification.

The existing per-kind restart and abort-recovery algorithms remain; transient
supervisor/process failures are real even without mixed-generation imports. Only
the purge/reload workaround and independent retry/ZIP tail compositions disappear.
Gateway exit status is written before a restart can terminate the updater's cgroup,
and is demoted on later failure. Verification publishes the final receipt.

## Parent lifecycle and failures

The parent waits and propagates the child's exact nonzero result (a signal is
mapped to shell-style 128+signal). A child cannot succeed by merely exiting zero:
a terminal response with the matching receipt identity is required. The response
returns the mutated Windows token so the parent's registered emergency resume does
not repeat completed work. Normal parent completion performs no maintenance.

The parent retains its original receipt until acknowledged child finalization;
missing/failed child output leaves it available to the existing command-boundary
failure finalizer. The stdlib bootstrap returns correlated PM failure data even
when application imports are unavailable, and normalizes negative signal exits
at each process boundary. POSIX completion owns a new session/process group;
cancellation kills that group before releasing the lock (Windows uses the retained
child's `taskkill /T` tree). The parent records the pending fleet obligation before
starting the completion process, including when preparation cannot begin. The parent's emergency Windows resume remains a last-resort
lifecycle obligation when the child cannot execute or is killed. A failed child
never clears the pending fleet obligation. No automatic code rollback after
maintenance has begun (SQLite snapshots remain file-loss recovery, not rollback).

## Historical surface

All names frozen from the complete reachable shipped updater history stay
resolvable. Historical dependency hooks retain the stdlib-only takeover bridge:
the old parent waits, carries receipt/recovery state and never resumes a retired
installer. Newly retired preparation and module-reload hooks explicitly marked
incomplete stop nonzero and request `hermes update` again; they cannot manufacture
a missing completion request. Current Git/current/ZIP callers use only the
canonical completion transport, not the historical takeover entrypoint.
Unfrozen branch-only retry compositions are deleted, not shimmed. ACP convenience
publication uses the launcher owner's `expose_cli`; the historical ACP entry is
only an adapter, never a second writer. The frozen set is never trimmed or replaced
with tag-only coverage. New current-path imports are unioned with that history.

## Verification

Use isolated homes, disposable Git repositories and fake dependency/build/service
adapters only. Exercise an old process with cached incompatible modules across a
real Git transition to new code, selected-Python execution, receipt identity and
snapshot transfer, nonzero/abrupt child exit, lock release and Windows-token
return. Focused existing tests cover dirty ZIP checks/grafts, snapshots, fleet
reconciliation, supervisor timing and historical imports. Native service restart
and Windows/macOS acceptance remain separate required lanes; no live user service
or user state is touched by this implementation's test runs.

The launch-completion unit tests keep real launcher publication inside temporary
homes but replace `_register_windows_user_path` in their autouse fixture. A
scratch `HERMES_HOME` alone does not isolate `HKCU\Environment\Path`: successful
publication otherwise persists that scratch home's `bin` in the operator's User
PATH. The three existing first-launch/completion/adoption tests assert the native
Windows registration request against the fixture's recorded entries. Normal
installer registration and the upper-level opt-in completion fixture are unchanged;
previously persisted scratch entries require a separate, explicitly scoped cleanup.

Local verification on 2026-09-30 (UTC): the three existing tests reproduced the
registration requests with a boundary double and passed again after autouse
isolation; all 20 tests in `test_update_launch_completion.py` passed through
`scripts/run_tests.sh`. Focused base/head ruff and ty diagnostics were both zero.
The raw HKCU PATH value and registry value type had identical SHA-256 fingerprints
before reproduction and after validation; no existing PATH entries were cleaned.

## Observational Codex provider status preparation

The exact parsed command `auth status openai-codex` verifies an already committed
runtime instead of adopting an install, syncing dependencies, finishing source
completion or retrying early recovery. Missing/corrupt state, pending source or
publication work, stale dependencies and probe errors exit nonzero with
`runtime-not-ready`. Help/version, update and other commands retain their existing
preparation and compatible-generation fallback. A ready status probe can re-enter
the selected managed Python for ABI compatibility without publishing launchers.

Dependency currency still uses PM's canonical `venv_is_current` stamp comparison
(core lock, Python pin/target, extras and the selected plugin union). Its worker
operation retains `bootstrap=never`. This command alone starts one ten-second
monotonic preparation deadline before the facts/input snapshot and passes that
same deadline through PM resolution, validation, worker response/cleanup and
observational activation. ABI re-entry carries it in ephemeral interpreter state,
not persisted configuration or a new environment variable, so a child does not
receive a fresh budget. Late responses and exhausted preparation fail closed;
there is no install, sync, completion or recovery retry. Bounded PM-runtime failures
remain errors instead of being reported as ordinary unknown/stale currency.

The inspected call graph and blocking boundaries are:

| Stage | Existing owner/boundary | Status deadline behavior |
| --- | --- | --- |
| Snapshot and interpreter/tool selection | bootstrap, `venv_sync`, PM `runtime_command`/`runtime_python`, `_toolchain(realize=False)` | Same deadline; elapsed local metadata reads checked before subsequent waits. |
| Prepare lock | `pm/runtime.py:prepare_runtime` → `pm/filesystem.py:lock_fd` | Remaining budget minus one-second cleanup reserve; contention fails without bypass/deletion. |
| PM import validation | `pm/runtime.py:_validate` | Owned subprocess and exclusive temporary stderr transport; bounded wait/kill/cleanup instead of `run`/`communicate`. |
| PM generation pin | `_hold_for_children` → existing `runtime_state.lease_directory` | Same remaining lock/retry budget, normal lifetime ownership. |
| Worker spawn, queue/reader and currency computation | `pm/client.py:_request` → `worker.py` | Parent supervises the entire worker body, including its queue, lease and metadata computation; no client reader/monitor thread. |
| Response and cleanup | `_request` owned process wait/kill/reap, temporary streams | Response uses remaining budget minus reserve; cleanup uses only remaining deadline, never a fresh timeout. |
| Publication and app generation pin | `activate_dependencies` → existing runtime lock/lease | Publication lock tries once; lease gets remaining budget. Pending journal or state/input drift fails, without recovery. |
| Dependency import activation | `site.addsitedir` and selected `.pth` code | State/deadline checked before and after; arbitrary trusted Python cannot be interrupted by these checks. |
| ABI re-entry | `relaunch_command`, Windows `subprocess.call` or POSIX `execv` | Child receives the original preparation deadline; Windows parent still waits for the whole auth command. |

Ordinary requests retain their pipe/callback protocol, default PM validation timeout
and normal install/recovery/activation behavior. The one-second reserve and existing
50ms lock polling bound the cooperative waits, not arbitrary synchronous OS file or
process creation, trusted `.pth` execution, scheduling delays, or a failed OS kill.
Those are checked after returning; this is not a hard wall-clock guarantee for the
whole launcher/auth handler, nor a process-tree supervisor. Ownership remains with
each existing lock/lease/process; only its own process is killed and only its own
failed temporary lease/transport is removed.

The bootstrap retains facts bytes and PM input mtimes in memory from before the
probe. Observational activation tries the existing publication lock immediately;
contention, pending journals, or facts/input drift before or after leasing fail
before dependency imports. It performs no publication recovery/write. A failed
post-lease check releases that lease; successful activation retains the normal
generation lifetime lease. Input mtimes detect intervening edits, not currency:
the canonical PM stamp remains the readiness authority. No new metadata store or
lock is introduced.

The bounded worker uses exclusively created standard-library temporary streams,
closed/deleted on success and exceptions, with existing host temporary-file
permissions (POSIX mode 0600; Windows host ACLs). The request carries PM operation,
dependency inputs, import identities/paths and correlation metadata, not auth
tokens or credentials. There are no client pipe-reader/monitor threads, persistent
transport files, new servers or process-wide kill operations. This preparation-only
change does not establish read-only behavior for the entire CLI. The auth/helper
follow-up below addresses its separate credential and config maintenance.

Local verification uses only the two existing launch/bootstrap test boundaries,
temporary homes, fake PM/process/auth/registry/network adapters and a virtual
clock. It covers currency success/stale/pending/missing/corrupt/error, no/late
worker response, bounded owned-worker cleanup, unchanged default requests,
publication/selection/input races, lock contention, normal lease and ordinary
activation/fallback. The earlier synthetic stall showed a preparation risk, not
the historical SkillWave timeout's proven cause. Actual auth status, natural
SkillWave recovery and service/runtime adoption were not executed for this change.

Verification for this status change on 2026-09-30 (UTC): 39 launch cases plus the
new managed-Python handoff case passed, alongside the six affected bootstrap
failure/normal cases and six existing entrypoint-order cases. All ran through
`scripts/run_tests.sh`. Base/head ruff diagnostics were zero; ty retained the same
23 pre-existing diagnostics, with no new issue. The post-cleanup raw HKCU PATH
and type fingerprint stayed `2529b2b471de856ca206fe20dfe57bb7a06c0e9afa8b89f824a2795ccc1fe3d0`.
No additional registry cleanup was performed by this change.

Follow-up verification on 2026-09-30 (UTC): the previous `_worker_command` test double
had hidden the PM prepare lock and validation waits. The fixture now traverses the
production resolver/preparation chain with temporary pinned tool/PM/app metadata;
only the OS kernel lock, subprocess and clock behavior is simulated. Canonical
currency comparison also runs in the fixture. Prepare-lock and PM/app lease stalls
end at about 9.05 virtual seconds (one existing polling tick); validation/response
timeout plus owned cleanup ends at 9.25, delayed worker spawn plus cleanup at 9.75,
and longer successful cleanup at 9.9. Normal currency/status preparation and
activation complete at 0.5 virtual seconds. These are synthetic measurements, not
the historical SkillWave event or a natural execution receipt. The selected real
installation preparation path was inspected again; no actual PM/auth probe,
installation, network call, service restart or registry mutation was executed.

The follow-up native checks passed 49 launch cases and 25 affected bootstrap cases
(74 distinct cases) through `scripts/run_tests.sh`. After fixture typing cleanup,
the 29 status preparation and seven status bootstrap cases passed again; the final
three transport/normal-status cases also passed. The per-file runner's shared
JUnit output path contains only the last file of a combined run, so its existing
runner log supplies that run's 36-case aggregate. Base/head ruff stayed 0/0; ty
stayed 23/23 existing diagnostics (19 stable keys), with no new diagnostic. Exact
diff/syntax checks passed. The HKCU PATH raw value/type hash remained `2529b2...`;
no registry/PATH cleanup or runtime activation was performed. The installation is
editable against this source; already-running processes were not restarted and
natural SkillWave recovery remains unobserved.

### Core Codex auth status observation

Core `auth status openai-codex` helpers observe stored credentials from the first
read. Status no longer runs pool maintenance before querying: the observational
pool load/peek neither heals profile forks, seeds a singleton, prunes old DEAD
rows, rotates selection/counts nor refreshes/adopts external credentials. The
existing read-only resolver also passes its policy through store/global fallback
and quota metadata reads. A corrupt/unreadable auth store reports an error rather
than copying, replacing or repairing it. Stored login metadata is not proof that
an expired token currently works. Normal runtime healing, seeding, pruning,
refresh and recovery retain their defaults.

Status config consumers use `load_config_readonly(observe_only=True)`, reusing
the existing effective merge, environment expansion, managed overlay, explicit
endpoint ownership and last-known-good recovery. This policy skips home/SOUL
initialization and good/corrupt backup publication, including the logged-out
external-login notice. It does not prime ordinary loader caches. An observational
parse warning has a separate deduplication key so it cannot suppress a later
runtime warning/backup. Ordinary config loads still initialize and back up as
before; no new config store or provider/grant policy was introduced.

On 2026-09-30 UTC, the final six selected files passed 80 cases through
`scripts/run_tests.sh`: 34 status/config observations plus 46 existing profile-fork,
runtime recovery, auth read-failure, config backup and cache-lock controls. All
authentication/network boundaries used fake data/adapters and temporary homes.
Ruff was 0/0 against the fixed parent; type checking had no new stable diagnostic
key, while existing diagnostics remain. The existing pinned-mtime cache case
failed with both the SHA-verified parent config source and this source in isolated
homes; that known failure remains outside this patch. Its one-time comparison
code was removed before commit. Exact diff checks passed and the HKCU PATH
value/type fingerprint remained `2529b2b471de856ca206fe20dfe57bb7a06c0e9afa8b89f824a2795ccc1fe3d0`.

This is a helper boundary result. SkillWave invokes the complete CLI via
`skill_wave_runtime_contract.py::run_automation_auth_preflight`, so it also pays
the earlier `main.py` startup path. That path still reads `config_effective`
with good-backup side effects, configures diagnostic files, sweeps shim/bytecode
artifacts and, after a revision change, runs `BOOT_HOME_STEPS` (including config
migration, plugin-cache invalidation, launcher/PATH exposure and a boot record).
These startup owners were inspected but not changed here. The existing exact
`venv_sync.is_auth_status_probe()` predicate is the proposed boundary for a
separate minimal startup observation policy; normal startup/update behavior must
remain intact. User-installed provider auth handlers keep their own contracts.
Actual auth status, whole-CLI safety and natural SkillWave recovery were not
executed or established. The installation is editable; running processes were
not restarted and runtime adoption is not inferred from this source commit.
