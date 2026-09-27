# Decisions

## D-001: Sync strategy
We use cold sync only.
Sync happens only when Codex is fully closed.

## D-002: Legal/safety boundary
The project only works with user-local files.
No token handling, no network interception, no reverse engineering.

## D-003: Scope
The project is a utility, not a Codex plugin.

## D-004: Storage
Cloud folder can be OneDrive, Dropbox, Syncthing, Google Drive mirror, etc, or a network folder.

## D-005: Conflict policy
Single-writer assumption.
User should not actively work in Codex on two machines at the same time.

## D-006: Initial platform
Windows first.

## D-007: Operational handoff contract
The expected workflow is strict and manual:
close Codex on machine A, wait for cloud propagation, then sync on machine B.

## D-008: Responsibility boundary for cloud environment
The utility does not verify cloud client process status or free space in cloud/network storage.
These checks are out of scope and owned by the user.

## D-009: CI target matrix for MVP
CI runs on Windows and macOS runners (`windows-latest`, `macos-latest`).
Linux CI is intentionally disabled for MVP until Linux runtime support is explicitly in scope.

## D-010: Preflight diagnostics mode
The CLI provides `doctor` and `preflight` commands (equivalent behavior).
These checks are read-only and validate runtime readiness before sync:
- config/runtime path readiness
- local/cloud/backup/temp readability and directory shape
- Codex process precondition
- manifest data-version compatibility
- session catalog audit (invalid/ambiguous sessions, graph codes)
- read-only SQLite audit
- orphan temp file detection

If any preflight check fails, the command exits with code `5` (`fail-safe`).

Amendment (0.2): the original list promised write-access probes and a
local/cloud mtime-drift probe. Both wrote probe files, one of them inside the
Codex state directory. `OperationKind.DOCTOR` is declared `side_effect_free`,
so the drift probe was dropped and the path checks were reduced to readability
checks. Drift detection may return later, but only in a form that writes
nothing into state.

## D-011: Bounded retry on a locked destination
`os.replace` is the commit step of every mutation. Destinations live in
directories a cloud client, search indexer or antivirus may open at any moment,
which on Windows surfaces as `WinError 5`/`32`/`33` for as long as that handle
lives — usually milliseconds.

Treating this as a hard failure aborted the run and left a `RECOVERY_REQUIRED`
journal for a condition that had already cleared, which is a worse outcome than
waiting. The engine therefore retries a *transient lock* up to five times with
exponential backoff (~1.5s total) before giving up.

This does not weaken `fail-safe`:
- every attempt is the same atomic `os.replace`, so a destination is never
  partially written;
- the destination hash is still verified after the replace;
- the process-safety check is re-proved before each further attempt, so Codex
  starting during the wait still stops the commit;
- errors that are not a transient lock (for example `EXDEV`) are raised on the
  first attempt, unretried.

Retry counts are constants, not configuration: they are a property of the
filesystem behaviour, not a user preference.

## D-012: One-way sync directions are configurable
`sync.direction` accepts `bidirectional` (the default), `to_cloud` and
`to_local`. The one-way modes exist because a machine is often only a source or
only a destination during a handoff, and copying the other way at that moment
is exactly what a person wants to prevent.

Nothing about the cold-sync model changes: a one-way run still needs Codex
stopped, still backs up before overwriting and still refuses on uncertainty.
Two properties keep it honest:

- **A skipped action is never recorded as synchronised.** The manifest holds a
  two-sided fingerprint so that a one-sided change can be told from a conflict.
  Writing the skipped side's current fingerprint would make the next
  bidirectional run believe both sides had agreed, and it would then take the
  older file for the newer one. For every path the direction skipped, the
  previous manifest entry is carried over unchanged.
- **A conflict stays a conflict.** When both sides changed, a one-way run does
  not quietly overwrite the other side; `conflict.policy` decides, exactly as
  in a bidirectional run.

`validate`, `doctor` and the run report state the direction, so a run that
copied nothing in one direction says why.

Amendment (CS-288): "the manifest" is one baseline per machine. The file lives
in the shared workspace, and a single two-sided entry made machine B take
machine A's last sync for its own and copy its older file over A's newer one.
Each machine now reads and writes only its own pair (local and cloud as *it*
saw them); a skipped path carries this machine's previous entry. An entry from
the unkeyed pre-0.2 format is attributed to nobody, which makes the first run
after the upgrade a first sync. Any conflict left in a plan stops the run,
whatever `conflict.policy` is (CS-295).

## D-013: Deletions may be propagated, but only against proof
`sync.delete_policy` accepts `never` (the default) and `propagate`.

Under `propagate`, a file missing on one side is deleted on the other **only
when the previous manifest proves that both sides held it and the surviving
side has not changed since**. Anything else — no manifest, an unknown path, a
side that changed after the recorded fingerprint — is a conflict, not a
deletion. The first run after enabling it therefore deletes nothing, because
there is no proof yet.

The safety rules are unchanged and apply in full (`AI_RULES` 3 and 6):

- a verified backup of the file is created before it is removed, and the
  removal is logged as its own dangerous action;
- the deletion runs inside the same mutation envelope as every other write —
  operation lock, journal, gate re-checked before each step — so `recover` can
  roll it back from that backup;
- a dry run deletes nothing;
- semantic-owned paths (`sessions/`, `archived_sessions/`, the session index,
  the global state, SQLite) are never deleted by `sync`. Moving a session to
  the archive is a separate question (`ARCHIVE_TRANSITION`) and this setting
  does not open it.

Amendment (CS-288, CS-324): the proof is this machine's own baseline, never
another machine's, so an entry the other machine wrote cannot delete a file
here. And a deletion out of an include root that holds no file at all on the
side it went missing from is a conflict: an empty or missing root is far more
likely a folder being re-downloaded or a disconnected drive than a person
deleting each file. Paths that differ only in letter case are a conflict too.

## D-014: A person may accept a suspicious shrink as Guardian's new baseline
Guardian quarantines a state whose project or binding count fell sharply
(`guardian_shrink`), and nothing suspicious becomes `latest-good` on its own.
That rule stays. What it did not cover is a drop that is real: the comparison is
always against `latest-good`, so after one every later state is suspicious too
and `latest-good` never moves again. Observed on 2026-09-13, when the desktop
build re-created all 16 projects under new ids and the 6 bindings naming the old
ids disappeared; the store stayed on the 2026-09-05 snapshot.

`guardian accept` is the sanctioned way out, and its limits are the decision:

- Only `SUSPICIOUS` with a shrink code can be accepted. `INVALID` and
  `INDETERMINATE` cannot, whatever is confirmed: acceptance overrides a
  judgement about counts, never an integrity check.
- The preview explains the drop in counts only (projects replaced — gone while
  a project with the same roots exists under a new id — removed and added; lost
  bindings attributed to each). Names, roots and thread ids stay in core, as
  everywhere else in Guardian.
- The plan id covers the baseline (id and hash), the counts, the shrink codes
  and a digest of which bindings and projects went and why, but not the state's
  own hash. Codex rewrites the file every few minutes; an id that followed the
  bytes could not be confirmed while Codex runs, which is when Guardian works. A
  different drop is a different id.
- The acceptance runs the watcher's pipeline under the runner lock (stable
  reads, validation, shrink assessment) and commits through the ordinary store
  path. The manifest is `PASS_WITH_WARNING` with the shrink codes plus
  `SHRINK_ACCEPTED`, and names the overridden baseline as its predecessor.
- Retention never prunes an accepted snapshot or the baseline it overrode.
- It writes only into the Guardian root, so it is allowed while Codex is open.
  `doctor` warns when shrink quarantines are newer than `latest-good`.

## D-015: A record-format rewrite by Codex is a conflict decided in bulk
The session model assumed a history only grows: a branch changes at its end or
not at all, which is what makes a prefix a fast-forward and anything else a
divergence. The September 2026 desktop build broke that itself. It rewrote every
existing session file into numbered records (`ordinal` on each record, messages
moved into `item` payloads, `session_meta` carrying `session_id` and
`history_mode`), kept each file's mtime, and dropped records on the way: turns
the person had rolled back, repeated `session_meta` records, injected
instructions and most of the guardian sub-agent reviews. Observed on 2026-09-19:
all 277 local sessions rewritten, the mirror written on 2026-09-05 still holding
242 of them in the old format, and every one of those a
`DIVERGED_NO_COMMON_RECORDS` that refused `sessions apply` as a whole.

What was decided:

- It stays a conflict. The two copies are not the same history, and proving
  "same history, re-encoded" would mean trusting a projection of one format onto
  the other that the dropped records already contradict. The catalogue records
  each branch's record format (`legacy`, `ordinal`, `mixed`) and the latest
  record time; a conflict between two formats is labelled `FORMAT_MIGRATION`
  with the newer side, and nothing else about it changes — not its conflict id,
  not the fact that it blocks.
- One explicit decision covers all of them. `sessions resolve
  --format-migrations` writes an ordinary pinned resolution keeping the newer
  side for each. It never decides a conflict whose older copy has a record later
  than anything in the newer one (`OLDER_FORMAT_HAS_LATER_RECORDS`): that copy
  may hold work done elsewhere before the upgrade.
- The loser is kept, alone and compressed. A conflict bundle stores both raw
  branches; here the winner is what the destination is about to hold, and both
  copies of 242 sessions would have been about two gigabytes written into a
  folder the config allows to be in the cloud. `superseded/<branch sha256>/`
  holds the old branch in the mirror's container, verified by decompressing it
  before the directory is committed. It is the only surviving copy of what the
  rewrite dropped, and nothing prunes it.
- `doctor` reports both sides' formats from each file's first record
  (`session_format`), so a rewrite that reached one side is a known step rather
  than two hundred unexplained conflicts.

## D-016: One settings sync may run unattended, once, after sign-in
CS-232 made every scheduled job read-only: a task runs while nobody watches,
and each mutation has its own plan and an explicit confirmation. On the owner's
machine Codex stays open until shutdown, so the only cold window is right after
sign-in, and asking for a manual sync then is asking for it to be forgotten.

What was decided (2026-09-23, by the owner):

- It is opt-in: `[scheduler] sync_at_login`, a checkbox, off by default and
  independent of the periodic job.
- It is one job and it runs once: `sync --apply --unattended` on a sign-in
  trigger in a task of its own (`LOGIN_SYNC_SLOT`), never repeated, never a
  periodic mode, so switching it off removes exactly that task.
- It is the plain settings sync only, because that is the one mutation that
  needs no plan id — its envelope (gate, lock, journal, verified backup) does
  not depend on a person. Sessions, restore, repair and moves keep their
  confirmation and are never scheduled.
- `--unattended` forces `manual_abort` on conflicts before planning, whatever
  `conflict.policy` says: nobody is there to decide one.
- The process gate is not relaxed. A Codex that starts with the session makes
  the run a refusal (exit 3) rather than a race, and the window reports the
  task's last result in words.
- Installing it must not run it: the Windows logon trigger and systemd's
  `OnStartupSec` do not fire at install, and the LaunchAgent is written but not
  bootstrapped, since loading it would fire `RunAtLoad`.

## D-017: A copy of `.codex` may be taken on a schedule, and it waits for Codex
Asked for on 2026-09-24 as "automation of backups", and specified by the owner
on 2026-09-25: a copy of the Codex state at sign-in and/or on a timer, which
waits when Codex is open rather than copying it while it runs.

What was decided:

- It is a new, separate job (`state-backup create --wait`) in a task of its own
  (`STATE_BACKUP_SLOT`), not a periodic `[scheduler]` mode and not a variant of
  the pre-overwrite backups, which exist only inside a write.
- It reads `.codex` and writes only into `[state_backup] root_dir`, which the
  user chooses; nothing proposes a location (a cloud folder would upload every
  copy). Empty means off, and a schedule without a folder is a config error.
- What is copied is the valuable part — sessions, archive, global state and its
  `.bak`, the SQLite catalogues with `-wal`/`-shm`, the session index,
  `config.toml`, `AGENTS.md`, rules, skills, memories, automations. Secrets are
  refused by name at any depth (`sync_candidates.SECRET_NAMES`); caches, logs,
  the sandbox and temporaries are left out.
- It goes through the safety gate as `OperationKind.STATE_BACKUP`, which needs
  Codex stopped without being a mutation: a copy of files being written is a
  copy of no moment. The task waits (30 s polls, up to 23 h, under a 24 h OS
  limit); the window and `create` without `--wait` refuse instead. A file whose
  size or mtime moves while it is read fails the copy, and the gate is asked
  again before the copy is committed.
- One verified zip per copy, written as `.partial` and renamed only after every
  entry reads back with the hash it was written with. Only this machine's copies
  beyond `keep` (zip, keep 5 by default) are removed, only by the exact name
  pattern, and only after the new copy is committed.
- Restoring from a copy is by hand. A restore into `.codex` would be a mutation
  with its own plan and confirmation, and none was asked for.

Amendment (2026-09-27, review CS-301): Codex's own `config.toml` stays in the
copy, by the owner's decision. It can hold MCP server tokens (`env` tables,
bearer headers), which `SECRET_NAMES` cannot see because it works by file name;
leaving it out would lose the one file that says how Codex was set up, and
filtering its keys would be a guess about a format codexSync does not own. The
risk is accepted and written in the user documentation: the copy folder must
be private. Links (symlinks and junctions) are not followed, and a folder that
cannot be listed fails the copy rather than leaving a hole in it.
