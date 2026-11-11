# SHM extraction, 2026-10-08

`source-manifest.json`: 50 live-working-tree source files copied and SHA256 verified before changes (including uncommitted sources). Exact original bytes are in the external archive named by `shm-registration.json`; no original code snapshot is shipped as a second implementation.

48 transferred/superseded source files removed from SHM after archive verification. `team/.gitignore`, `defect-classes.md`, `go_test_sharded.py`, business requirements, tracking, pilot/verify reports and historical logs stay in SHM. Project-specific checks from common instructions/templates consolidated in SHM `team/PROJECT.md`; registered profile in `team/profile.json`.

Historical Dispatch authority was checked through Orca: all settled, capabilities revoked; resources released or user-owned. A live historical terminal had `retainedReason=user_takeover` and was left untouched. Old state was never imported or replayed. No working queue was launched. No commits or branch changes.

Old `~/.local/state/shm-team/runs` and caches are historical project artifacts; they were not deleted or copied into a new executable state namespace. The read-only archive contains the old goal-runner state/config and every inventoried source, with paths, hashes and original IDs.

Feature tracking and evidence now live in this repository: [Team Runner extraction](../feat/team-runner-extraction/team-runner-extraction.md) and [original Goal Runner UI](../feat/goal-runner-ui/goal-runner-ui.md). Historical receipts and statuses are preserved as recorded.
