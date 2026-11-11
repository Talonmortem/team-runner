# Verification — 2026-10-08

- Original baseline: 45 unittest regressions + helper scripts PASS. Sandbox-only HTTP failures disappeared when loopback access was available.
- Final new repository: 53 unittest cases PASS. Includes all 45 migrated cases plus multi-project command/message isolation, canonical/symlink checkout locks, inherited-context conflicts, settings snapshots, bad Orca path/incarnation, unavailable checkout/Orca, archive refusal, HTTP security and CLI catalog lifecycle.
- Standalone helper suites: orca_chk, orca_msg, orca_recover, run_logged and token-usage PASS. Final transport-selection edits rechecked in the three affected helper suites.
- Two-project browser cycle PASS: UI project creation/checkout registration; concurrent runner processes; independent role settings and snapshots; questions; gate; pause/resume; independent server/runner restarts; stop/retry; lost-response unknown/reconcile; disable preserving history. Real HTTP and processes, isolated fake Orca transport. No real sber files accessed.
- Installed package HTTP/browser check on registered SHM PASS: only SHM registered, nine roles, empty run history/current, no working queue started. Test server and browser tab closed.
- Final Codex smoke PASS: ctx_72e72b51b3d5 → ctx_e251e873d9f9, measured Context 4%, same terminal, cleanup confirmed.
- Final Claude smoke PASS: ctx_a843b72ef22a → ctx_8aaa8a90fff4, measured /context 4%, same terminal, cleanup confirmed.
- Initial smoke harness had no Claude context probe and could mistake Codex weekly-limit percentages for context. Corrected diagnostic parsing; no guessed measurements used for reuse.
- SHM `go test ./...` PASS; `npm run build` PASS, existing large-bundle warning. Unrelated source changes preserved.
- Read-only archive checksum verified; source inventory preserved uncommitted originals. 48 shared source files removed from SHM; .gitignore and domain defect rules retained.
- Historical attempts settled with revoked capabilities. Released or user-owned resources were not adopted; the live user_takeover terminal was left intact.
- Read-only status, no duplicate shared implementation and `git diff --check` PASS.

No commits, pushes, branch switches, auto-starts, active-session imports or working goal launches.
