#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AI_DEV_REAL="${AI_DEV_BIN:-$(command -v ai-dev || true)}"
if [[ -z "$AI_DEV_REAL" ]]; then
	printf 'ai-dev command not found on PATH.\n' >&2
	exit 1
fi
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

FLOW_COMMANDS=(start patch status diff commit reset promote complete block resume)
FLOW_BIN_DIR="$TMP_DIR/flow-bin"
mkdir -p "$FLOW_BIN_DIR"
for flow_command in "${FLOW_COMMANDS[@]}"; do
	launcher="$FLOW_BIN_DIR/flow-$flow_command"
	{
		printf '%s\n' '#!/usr/bin/env bash'
		printf 'FLOW_COMMAND_NAME="flow-%s" PYTHONPATH="%s" exec python3 -m ai_dev_flow.cli __ai_dev_flow_exec__ "%s" "$@"\n' "$flow_command" "$ROOT" "$flow_command"
	} >"$launcher"
	chmod +x "$launcher"
done
export PATH="$FLOW_BIN_DIR:$PATH"

fail() {
	printf '%s\n' "$1" >&2
	exit 1
}

assert_contains() {
	local haystack="$1"
	local needle="$2"

	if [[ "$haystack" != *"$needle"* ]]; then
		printf 'expected output to contain: %s\n' "$needle" >&2
		printf 'actual output:\n%s\n' "$haystack" >&2
		exit 1
	fi
}

assert_not_contains() {
	local haystack="$1"
	local needle="$2"

	if [[ "$haystack" == *"$needle"* ]]; then
		printf 'expected output not to contain: %s\n' "$needle" >&2
		printf 'actual output:\n%s\n' "$haystack" >&2
		exit 1
	fi
}

assert_equals() {
	local left="$1"
	local right="$2"

	if [[ "$left" != "$right" ]]; then
		printf 'expected: %s\nactual:   %s\n' "$right" "$left" >&2
		exit 1
	fi
}

init_repo() {
	local repo_root="$1"

	mkdir -p "$repo_root/subdir"
	mkdir -p "$repo_root/.ai-dev/tickets"
	cat >"$repo_root/.ai-dev/config.json" <<'EOF'
{
	"tickets": {
		"provider": "local",
		"path": ".ai-dev/tickets"
	},
	"review": {
		"promotionGate": false
	}
}
EOF
	for ticket_id in $(seq 1 200); do
		cat >"$repo_root/.ai-dev/tickets/${ticket_id}.json" <<EOF
{
	"reference": {
		"provider": "local",
		"ticketId": "${ticket_id}",
		"path": ".ai-dev/tickets"
	},
	"title": "Ticket ${ticket_id}",
	"lifecycleState": "open",
	"workflowState": "inactive"
}
EOF
	done

	(
		cd "$repo_root"

		git init -q
		git config user.name 'Flow Lifecycle Tests'
		git config user.email 'flow-lifecycle-tests@example.com'

		printf '.ai-dev/workflow.json\n.ai-dev/blocked-workflows.json\n.ai-dev/config.json\n.ai-dev/tickets/\n' > .gitignore
		printf 'base\n' > tracked.txt
		printf 'keep\n' > subdir/.keep

		git add .gitignore tracked.txt subdir/.keep
		git commit -q -m 'initial commit'
		git branch -M main
	)
}

run_flow() {
	local cwd="$1"
	shift
	local flow_command="$1"
	shift

	(
		cd "$cwd"
		PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" "flow-$flow_command" "$@"
	)
}

make_gh_mock() {
	local mock_bin_dir="$1"
	mkdir -p "$mock_bin_dir"
	cat >"$mock_bin_dir/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

python3 - "$@" <<'PY'
import json
import os
import sys
from pathlib import Path


def fail(message):
	print(message, file=sys.stderr)
	sys.exit(1)


def load_state(path):
	if not path.exists():
		return {'issues': {}}
	return json.loads(path.read_text(encoding='utf-8'))


def save_state(path, data):
	path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


state_path_value = os.environ.get('GH_MOCK_STATE')
if not state_path_value:
	fail('GH_MOCK_STATE is required')
state_path = Path(state_path_value)
state_path.parent.mkdir(parents=True, exist_ok=True)

state = load_state(state_path)
if 'issues' not in state or not isinstance(state['issues'], dict):
	state = {'issues': {}}

args = sys.argv[1:]
if len(args) < 3 or args[0] != 'issue':
	fail('unsupported command')

command = args[1]
issue_number = args[2]

if command == 'close':
	if issue_number not in state['issues']:
		fail('issue not found')
	state['issues'][issue_number]['state'] = 'closed'
	save_state(state_path, state)
	print(f'closed {issue_number}')
	sys.exit(0)

fail('unsupported issue subcommand')
PY
EOF
	chmod +x "$mock_bin_dir/gh"
}

write_gh_state() {
	local state_path="$1"
	local payload="$2"
	cat >"$state_path" <<EOF
$payload
EOF
}

read_gh_issue_state() {
	local state_path="$1"
	local issue_number="$2"
	python3 - "$state_path" "$issue_number" <<'PY'
import json
import sys

state = json.loads(open(sys.argv[1], 'r', encoding='utf-8').read())
issue = state.get('issues', {}).get(sys.argv[2], {})
issue_state = issue.get('state', '')
if not isinstance(issue_state, str):
	issue_state = ''
print(issue_state)
PY
}

read_local_ticket_lifecycle() {
	local repo_root="$1"
	local issue_number="$2"
	python3 - "$repo_root/.ai-dev/tickets/${issue_number}.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as handle:
	print(json.load(handle).get('lifecycleState', ''))
PY
}

state_get() {
	local cwd="$1"

	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$AI_DEV_REAL" __test-state-get
	)
}

current_branch() {
	local repo_root="$1"
	git -C "$repo_root" branch --show-current
}

branch_head() {
	local repo_root="$1"
	local branch_name="$2"

	git -C "$repo_root" rev-parse "$branch_name"
}

branch_tree() {
	local repo_root="$1"
	local branch_name="$2"

	git -C "$repo_root" rev-parse "$branch_name^{tree}"
}

head_message() {
	local repo_root="$1"
	git -C "$repo_root" log -1 --format=%B
}

repo_status() {
	local repo_root="$1"
	git -C "$repo_root" status --porcelain --untracked-files=all
}

assert_repo_clean() {
	local repo_root="$1"
	assert_equals "$(repo_status "$repo_root")" ''
}

mock_bin_dir="$TMP_DIR/mock-bin"
make_gh_mock "$mock_bin_dir"
gh_state_file="$TMP_DIR/gh-state.json"

lifecycle_repo="$TMP_DIR/lifecycle"
init_repo "$lifecycle_repo"
write_gh_state "$gh_state_file" '{
	"issues": {
		"123": {
			"state": "open"
		}
	}
}'

start_output="$(run_flow "$lifecycle_repo/subdir" start 123)"
assert_contains "$start_output" 'Started issue 123'
assert_contains "$start_output" 'mainBranch: main'
assert_contains "$start_output" 'scratchBranch: scratch'
assert_contains "$start_output" 'checkpoint: 0'

assert_equals "$(current_branch "$lifecycle_repo")" 'scratch'
assert_equals "$(branch_head "$lifecycle_repo" main)" "$(branch_head "$lifecycle_repo" scratch)"
assert_repo_clean "$lifecycle_repo"

status_after_start="$(run_flow "$lifecycle_repo/subdir" status)"
assert_equals "$status_after_start" $'Issue 123 — Ticket 123\nBranch: scratch'

printf 'checkpoint one\n' >> "$lifecycle_repo/tracked.txt"
printf 'new file one\n' > "$lifecycle_repo/one.txt"

diff_one_output="$(run_flow "$lifecycle_repo/subdir" diff)"
assert_contains "$diff_one_output" 'diff --git a/one.txt b/one.txt'
assert_contains "$diff_one_output" 'diff --git a/tracked.txt b/tracked.txt'

commit_one_output="$(run_flow "$lifecycle_repo/subdir" commit)"
assert_contains "$commit_one_output" 'Created checkpoint 1'
assert_contains "$commit_one_output" 'activeIssueNumber: 123'
assert_equals "$(head_message "$lifecycle_repo")" '1'
assert_repo_clean "$lifecycle_repo"

status_after_commit_one="$(run_flow "$lifecycle_repo/subdir" status)"
assert_contains "$status_after_commit_one" 'Issue 123'
assert_contains "$status_after_commit_one" 'Branch: scratch'
assert_contains "$status_after_commit_one" '1 commit ahead of main'

printf 'checkpoint two\n' >> "$lifecycle_repo/tracked.txt"
printf 'new file two\n' > "$lifecycle_repo/two.txt"

diff_two_output="$(run_flow "$lifecycle_repo/subdir" diff)"
assert_contains "$diff_two_output" 'diff --git a/two.txt b/two.txt'
assert_contains "$diff_two_output" '+checkpoint two'

diff_all_output="$(run_flow "$lifecycle_repo/subdir" diff --all)"
assert_contains "$diff_all_output" 'diff --git a/one.txt b/one.txt'
assert_contains "$diff_all_output" 'diff --git a/two.txt b/two.txt'
assert_contains "$diff_all_output" ' checkpoint one'
assert_contains "$diff_all_output" '+checkpoint two'
assert_contains "$diff_all_output" '+checkpoint one'

commit_two_output="$(run_flow "$lifecycle_repo/subdir" commit)"
assert_contains "$commit_two_output" 'Created checkpoint 2'
assert_equals "$(head_message "$lifecycle_repo")" '2'
assert_repo_clean "$lifecycle_repo"

main_before_promote="$(branch_head "$lifecycle_repo" main)"
scratch_tree_before_promote="$(branch_tree "$lifecycle_repo" scratch)"

promote_output="$(run_flow "$lifecycle_repo/subdir" promote 'Test complete workflow')"
assert_contains "$promote_output" 'Promoted scratch to main'
assert_contains "$promote_output" 'checkpoint: 0'
assert_contains "$promote_output" 'activeIssueNumber: 123'

main_after_promote="$(branch_head "$lifecycle_repo" main)"
scratch_after_promote="$(branch_head "$lifecycle_repo" scratch)"

assert_equals "$main_after_promote" "$scratch_after_promote"
assert_equals "$(git -C "$lifecycle_repo" rev-parse "$main_after_promote^")" "$main_before_promote"
assert_equals "$(branch_tree "$lifecycle_repo" main)" "$scratch_tree_before_promote"
assert_equals "$(head_message "$lifecycle_repo")" 'Test complete workflow'
assert_equals "$(current_branch "$lifecycle_repo")" 'scratch'
assert_repo_clean "$lifecycle_repo"

status_after_promote="$(run_flow "$lifecycle_repo/subdir" status)"
assert_equals "$status_after_promote" $'Issue 123 — Ticket 123\nBranch: scratch'

complete_output="$(run_flow "$lifecycle_repo/subdir" complete)"
assert_contains "$complete_output" 'Completed issue 123'
assert_contains "$complete_output" 'Workflow: inactive'
assert_contains "$complete_output" 'checkpoint: 0'
assert_equals "$(read_local_ticket_lifecycle "$lifecycle_repo" '123')" 'closed'

status_after_complete="$(run_flow "$lifecycle_repo/subdir" status)"
assert_equals "$status_after_complete" $'No active workflow.\nBranch: scratch'

assert_equals "$(state_get "$lifecycle_repo/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'

assert_equals "$(branch_head "$lifecycle_repo" main)" "$main_after_promote"
assert_equals "$(branch_head "$lifecycle_repo" scratch)" "$main_after_promote"
assert_equals "$(current_branch "$lifecycle_repo")" 'scratch'
assert_repo_clean "$lifecycle_repo"

reset_repo="$TMP_DIR/reset-lifecycle"
init_repo "$reset_repo"

run_flow "$reset_repo/subdir" start 124 >/dev/null

printf 'checkpoint work\n' >> "$reset_repo/tracked.txt"
git -C "$reset_repo" add tracked.txt
run_flow "$reset_repo/subdir" commit >/dev/null

printf 'staged change\n' >> "$reset_repo/tracked.txt"
git -C "$reset_repo" add tracked.txt
printf 'unstaged change\n' >> "$reset_repo/tracked.txt"
printf 'untracked file\n' > "$reset_repo/untracked.txt"
mkdir -p "$reset_repo/untracked-dir"
printf 'nested\n' > "$reset_repo/untracked-dir/nested.txt"

reset_output="$(run_flow "$reset_repo/subdir" reset)"
assert_contains "$reset_output" 'Reset scratch to main'
assert_contains "$reset_output" 'checkpoint: 0'
assert_contains "$reset_output" 'activeIssueNumber: 124'

assert_equals "$(branch_head "$reset_repo" main)" "$(branch_head "$reset_repo" scratch)"
assert_equals "$(current_branch "$reset_repo")" 'scratch'
assert_repo_clean "$reset_repo"

if [[ -e "$reset_repo/untracked.txt" ]]; then
	fail 'reset did not remove untracked.txt'
fi

if [[ -e "$reset_repo/untracked-dir" ]]; then
	fail 'reset did not remove untracked-dir'
fi

reset_status="$(run_flow "$reset_repo/subdir" status)"
assert_equals "$reset_status" $'Issue 124 — Ticket 124\nBranch: scratch'

printf 'flow lifecycle tests passed\n'
