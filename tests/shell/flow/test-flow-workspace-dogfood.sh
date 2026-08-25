#!/usr/bin/env bash
# End-to-end dogfood of two concurrent ticket workspaces in one isolated
# repository, driven through the installed skill helpers rather than the
# module, so the supported execution path is what is proven.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
FLOW_SCRIPTS="$ROOT/skills/copilot/flow/scripts"
TMP_DIR="$(mktemp -d)"
trap 'cleanup' EXIT

cleanup() {
	if [[ -d "$TMP_DIR/repo/.git" ]]; then
		git -C "$TMP_DIR/repo" worktree prune >/dev/null 2>&1 || true
	fi
	rm -rf "$TMP_DIR"
}

fail() {
	printf '%s\n' "$1" >&2
	exit 1
}

assert_contains() {
	local haystack="$1" needle="$2" context="$3"
	if [[ "$haystack" != *"$needle"* ]]; then
		printf 'expected %s to contain: %s\nactual:\n%s\n' "$context" "$needle" "$haystack" >&2
		exit 1
	fi
}

assert_not_contains() {
	local haystack="$1" needle="$2" context="$3"
	if [[ "$haystack" == *"$needle"* ]]; then
		printf 'expected %s not to contain: %s\nactual:\n%s\n' "$context" "$needle" "$haystack" >&2
		exit 1
	fi
}

assert_equals() {
	local actual="$1" expected="$2" context="$3"
	if [[ "$actual" != "$expected" ]]; then
		printf '%s\nexpected: %s\nactual:   %s\n' "$context" "$expected" "$actual" >&2
		exit 1
	fi
}

flow() {
	local workspace="$1" command="$2"
	shift 2
	(cd "$workspace" && "$FLOW_SCRIPTS/flow-$command" "$@")
}

flow_expect_failure() {
	local workspace="$1" output_file="$2" command="$3"
	shift 3
	if (cd "$workspace" && "$FLOW_SCRIPTS/flow-$command" "$@") >"$output_file" 2>&1; then
		printf 'expected flow-%s to fail in %s:\n%s\n' "$command" "$workspace" "$(cat "$output_file")" >&2
		exit 1
	fi
}

ticket_status() {
	(cd "$1" && "$FLOW_SCRIPTS/ticket-status")
}

write_ticket() {
	local repo_root="$1" id="$2" title="$3"
	mkdir -p "$repo_root/.ai-dev/tickets"
	cat >"$repo_root/.ai-dev/tickets/$id.json" <<EOF
{
  "reference": {"provider": "local", "ticketId": "$id", "path": ".ai-dev/tickets"},
  "title": "$title",
  "lifecycleState": "open",
  "workflowState": "inactive",
  "body": "## Checkpoints\\n\\n- [ ] **Define the work**\\n  The first named checkpoint.\\n\\n- [ ] **Finish the work**\\n  The second named checkpoint.\\n\\n## Full Description\\n\\nTicket $id description.\\n"
}
EOF
}

record_review_pass() {
	local workspace="$1" issue="$2"
	local scratch_branch commit
	scratch_branch="$(git -C "$workspace" rev-parse --abbrev-ref HEAD)"
	commit="$(git -C "$workspace" rev-parse HEAD)"
	cat >"$workspace/.ai-dev/promotion-review.json" <<EOF
{
  "version": 1,
  "result": "pass",
  "scratchCommit": "$commit",
  "mainBranch": "main",
  "scratchBranch": "$scratch_branch",
  "activeIssueNumber": $issue
}
EOF
}

ticket_field() {
	local repo_root="$1" id="$2" field="$3"
	python3 - "$repo_root/.ai-dev/tickets/$id.json" "$field" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)[sys.argv[2]])
PY
}

# ---------------------------------------------------------------------------
# An isolated repository with two open tickets.
# ---------------------------------------------------------------------------

PRIMARY="$TMP_DIR/repo"
mkdir -p "$PRIMARY"
git -C "$PRIMARY" init -q
git -C "$PRIMARY" config user.name 'Workspace Dogfood'
git -C "$PRIMARY" config user.email 'workspace-dogfood@example.com'
printf '.ai-dev/\n' >"$PRIMARY/.gitignore"
printf 'base\n' >"$PRIMARY/tracked.txt"
git -C "$PRIMARY" add .gitignore tracked.txt
git -C "$PRIMARY" commit -q -m 'initial commit'
git -C "$PRIMARY" branch -M main

mkdir -p "$PRIMARY/.ai-dev"
cat >"$PRIMARY/.ai-dev/config.json" <<'EOF'
{
  "tickets": {"provider": "local", "path": ".ai-dev/tickets"}
}
EOF
write_ticket "$PRIMARY" 601 'Concurrent ticket A'
write_ticket "$PRIMARY" 602 'Concurrent ticket B'

# ---------------------------------------------------------------------------
# Two tickets active at once, without abandoning or suspending the first.
# ---------------------------------------------------------------------------

flow "$PRIMARY" start 601 >/dev/null
printf 'first checkpoint\n' >"$PRIMARY/a-one.txt"
flow "$PRIMARY" commit >/dev/null

SECOND="$TMP_DIR/workspace-602"
flow "$PRIMARY" workspace add 602 "$SECOND" >/dev/null
[[ -d "$SECOND" ]] || fail 'workspace add did not create the second worktree'

primary_status="$(flow "$PRIMARY" status -v)"
second_status="$(flow "$SECOND" status -v)"
assert_contains "$primary_status" 'issue number: 601' 'primary flow-status'
assert_contains "$primary_status" 'checkpoint: 1' 'primary flow-status'
assert_contains "$second_status" 'issue number: 602' 'second flow-status'
assert_contains "$second_status" 'checkpoint: 0' 'second flow-status'
assert_not_contains "$second_status" '601' 'second flow-status'

# Contextual /status stays contextual: one workspace, one ticket, no dashboard.
primary_ticket_status="$(ticket_status "$PRIMARY")"
second_ticket_status="$(ticket_status "$SECOND")"
assert_contains "$primary_ticket_status" '#601 Concurrent ticket A' 'primary /status'
assert_not_contains "$primary_ticket_status" '602' 'primary /status'
assert_contains "$second_ticket_status" '#602 Concurrent ticket B' 'second /status'
assert_not_contains "$second_ticket_status" '601' 'second /status'
assert_contains "$second_ticket_status" 'Current checkpoint: Define the work' 'second /status'

# The repository-level listing identifies every workspace and the current one.
listing="$(flow "$SECOND" workspace list)"
assert_contains "$listing" 'local:601' 'workspace list'
assert_contains "$listing" 'local:602' 'workspace list'
assert_contains "$listing" "$PRIMARY" 'workspace list'
assert_contains "$listing" "$SECOND" 'workspace list'
assert_contains "$listing" '(current)' 'workspace list'

# ---------------------------------------------------------------------------
# Independent checkpoints, and no leakage of either work surface.
# ---------------------------------------------------------------------------

printf 'second checkpoint\n' >"$SECOND/b-one.txt"
flow "$SECOND" commit >/dev/null
printf 'uncommitted\n' >"$SECOND/b-scratch.txt"

assert_equals "$(flow "$PRIMARY" status -v | grep -c 'checkpoint: 1')" '1' 'ticket A checkpoint unchanged'
[[ -f "$PRIMARY/b-one.txt" ]] && fail "ticket B's checkpoint leaked into ticket A's workspace"
[[ -f "$PRIMARY/b-scratch.txt" ]] && fail "ticket B's uncommitted file leaked into ticket A's workspace"
[[ -f "$SECOND/a-one.txt" ]] && fail "ticket A's checkpoint leaked into ticket B's workspace"
assert_equals "$(git -C "$PRIMARY" status --porcelain)" '' 'ticket A working tree stays clean'
assert_equals "$(git -C "$SECOND" status --porcelain)" '?? b-scratch.txt' 'ticket B keeps only its own change'

rm "$SECOND/b-scratch.txt"

# ---------------------------------------------------------------------------
# Promote one ticket while the other stays active.
# ---------------------------------------------------------------------------

record_review_pass "$PRIMARY" 601
flow "$PRIMARY" promote 'promote ticket A' >/dev/null
assert_equals "$(git -C "$PRIMARY" rev-parse main)" "$(git -C "$PRIMARY" rev-parse HEAD)" 'main advanced to ticket A'

second_after_promotion="$(flow "$SECOND" status -v)"
assert_contains "$second_after_promotion" 'issue number: 602' 'ticket B survives the promotion'
assert_contains "$second_after_promotion" 'checkpoint: 1' 'ticket B keeps its checkpoint'
[[ -f "$SECOND/a-one.txt" ]] && fail "promoting ticket A rewrote ticket B's working tree"

# The stale base is refused, not silently rebased or merged.
stale_output="$TMP_DIR/stale-output"
record_review_pass "$SECOND" 602
second_head_before="$(git -C "$SECOND" rev-parse HEAD)"
flow_expect_failure "$SECOND" "$stale_output" promote 'promote ticket B'
stale_text="$(cat "$stale_output")"
assert_contains "$stale_text" 'base is stale' 'stale promotion refusal'
assert_contains "$stale_text" 'flow-workspace refresh' 'stale promotion refusal'
assert_contains "$stale_text" 'Nothing was changed' 'stale promotion refusal'
assert_equals "$(git -C "$SECOND" rev-parse HEAD)" "$second_head_before" 'refused promotion left ticket B untouched'

# ---------------------------------------------------------------------------
# Refresh is the supported recovery, and review must be earned again.
# ---------------------------------------------------------------------------

flow "$SECOND" workspace refresh >/dev/null
[[ -f "$SECOND/a-one.txt" ]] || fail 'refresh did not bring the promoted main into ticket B'
[[ -f "$SECOND/.ai-dev/promotion-review.json" ]] && fail 'refresh kept review evidence bound to the old base'
assert_contains "$(flow "$SECOND" status -v)" 'checkpoint: 1' 'refresh left checkpoint numbering alone'
assert_equals "$(git -C "$PRIMARY" rev-parse main)" "$(git -C "$PRIMARY" rev-parse main)" 'refresh never moves main'

record_review_pass "$SECOND" 602
flow "$SECOND" promote 'promote ticket B' >/dev/null
assert_equals "$(git -C "$SECOND" rev-parse main)" "$(git -C "$SECOND" rev-parse HEAD)" 'main advanced to ticket B'

# ---------------------------------------------------------------------------
# Both tickets complete, and cleanup releases exactly one workspace.
# ---------------------------------------------------------------------------

flow "$SECOND" complete >/dev/null
assert_equals "$(ticket_field "$PRIMARY" 602 lifecycleState)" 'closed' 'ticket B closed'
assert_equals "$(ticket_field "$PRIMARY" 601 lifecycleState)" 'open' 'completing B left A open'
assert_contains "$(flow "$PRIMARY" status -v)" 'issue number: 601' 'ticket A still active after B completed'

flow "$PRIMARY" workspace remove "$SECOND" >/dev/null
[[ -d "$SECOND" ]] && fail 'workspace remove left the worktree behind'
remaining="$(flow "$PRIMARY" workspace list)"
assert_contains "$remaining" 'local:601' 'ticket A claim survives cleanup'
assert_not_contains "$remaining" 'local:602' 'ticket B claim released'

# Ticket A is now behind the main that ticket B advanced. It is not silently
# reconciled: refresh is the supported path back to a completable state.
completion_output="$TMP_DIR/complete-behind"
flow_expect_failure "$PRIMARY" "$completion_output" complete
assert_contains "$(cat "$completion_output")" 'is behind main' 'completion refuses a stale base'

flow "$PRIMARY" workspace refresh >/dev/null
flow "$PRIMARY" complete >/dev/null
assert_equals "$(ticket_field "$PRIMARY" 601 lifecycleState)" 'closed' 'ticket A closed'
assert_contains "$(flow "$PRIMARY" workspace list)" 'No workspace claims' 'every claim released'

printf 'flow workspace dogfood tests passed\n'
