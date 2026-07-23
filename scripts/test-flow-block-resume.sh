#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW="$ROOT/scripts/flow"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

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

assert_file_exists() {
	local path="$1"
	if [[ ! -f "$path" ]]; then
		printf 'expected file to exist: %s\n' "$path" >&2
		exit 1
	fi
}

assert_not_exists() {
	local path="$1"
	if [[ -e "$path" || -L "$path" ]]; then
		printf 'expected path to be absent: %s\n' "$path" >&2
		exit 1
	fi
}

init_repo() {
	local repo_root="$1"
	mkdir -p "$repo_root/subdir"
	(
		cd "$repo_root"
		git init -q
		git config user.name 'Flow Block Resume Tests'
		git config user.email 'flow-block-resume-tests@example.com'
		printf '.ai-dev/workflow.json\n' > .gitignore
		printf '.ai-dev/blocked-workflows.json\n' >> .gitignore
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
	(
		cd "$cwd"
		"$FLOW" "$@"
	)
}

run_flow_capture() {
	local cwd="$1"
	local output_file="$2"
	shift 2

	if run_flow "$cwd" "$@" >"$output_file" 2>&1; then
		return 0
	else
		local status=$?
		return "$status"
	fi
}

state_set() {
	local cwd="$1"
	local payload="$2"
	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$FLOW" __test-state-set "$payload"
	)
}

state_get() {
	local cwd="$1"
	(
		cd "$cwd"
		FLOW_TEST_MODE=1 "$FLOW" __test-state-get
	)
}

write_blocked_payload() {
	local repo_root="$1"
	local payload="$2"
	mkdir -p "$repo_root/.ai-dev"
	cat >"$repo_root/.ai-dev/blocked-workflows.json" <<EOF
$payload
EOF
}

repo_status_porcelain() {
	local repo_root="$1"
	git -C "$repo_root" status --porcelain --untracked-files=all
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

log_path_value = os.environ.get('GH_MOCK_LOG', '')

args = sys.argv[1:]
if len(args) < 3 or args[0] != 'issue':
	fail('unsupported command')

command = args[1]
issue_number = args[2]

if command == 'view':
	if os.environ.get('GH_MOCK_FAIL_VIEW') == '1':
		fail('mock view failure')
	if issue_number not in state['issues']:
		fail('issue not found')
	if len(args) != 5 or args[3] != '--json':
		fail('unsupported issue view syntax')
	fields = [field.strip() for field in args[4].split(',') if field.strip()]
	issue_data = state['issues'][issue_number]
	response = {}
	for field in fields:
		if field == 'labels':
			labels = issue_data.get('labels', [])
			if not isinstance(labels, list):
				labels = []
			response['labels'] = [{'name': str(label)} for label in labels]
		else:
			value = issue_data.get(field, '')
			if not isinstance(value, str):
				value = ''
			response[field] = value
	print(json.dumps(response))
	if log_path_value:
		with open(log_path_value, 'a', encoding='utf-8') as log_handle:
			log_handle.write(f"view:{issue_number}:{','.join(fields)}\\n")
	sys.exit(0)

if command == 'edit':
	if os.environ.get('GH_MOCK_FAIL_EDIT') == '1':
		fail('mock edit failure')
	if issue_number not in state['issues']:
		fail('issue not found')
	add_labels = []
	remove_labels = []
	i = 3
	while i < len(args):
		flag = args[i]
		if i + 1 >= len(args):
			fail('missing value for edit flag')
		value = args[i + 1]
		if flag == '--add-label':
			add_labels.extend([item.strip() for item in value.split(',') if item.strip()])
		elif flag == '--remove-label':
			remove_labels.extend([item.strip() for item in value.split(',') if item.strip()])
		else:
			fail('unsupported issue edit flag')
		i += 2
	labels = state['issues'][issue_number].get('labels', [])
	if not isinstance(labels, list):
		labels = []
	next_labels = [label for label in labels if label not in remove_labels]
	for label in add_labels:
		if label not in next_labels:
			next_labels.append(label)
	state['issues'][issue_number]['labels'] = next_labels
	save_state(state_path, state)
	print(f'edited {issue_number}')
	if log_path_value:
		with open(log_path_value, 'a', encoding='utf-8') as log_handle:
			log_handle.write(f"edit:{issue_number}:add={','.join(add_labels)}:remove={','.join(remove_labels)}\\n")
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

read_gh_labels_csv() {
	local state_path="$1"
	local issue_number="$2"
	python3 - "$state_path" "$issue_number" <<'PY'
import json
import sys

state = json.loads(open(sys.argv[1], 'r', encoding='utf-8').read())
issue = state.get('issues', {}).get(sys.argv[2], {})
labels = issue.get('labels', [])
if not isinstance(labels, list):
	labels = []
print(','.join(labels))
PY
}

read_blocked_reason() {
	local blocked_file="$1"
	local issue_number="$2"
	python3 - "$blocked_file" "$issue_number" <<'PY'
import json
import sys

data = json.loads(open(sys.argv[1], 'r', encoding='utf-8').read())
for item in data.get('blockedWorkflows', []):
	if item.get('issueNumber') == int(sys.argv[2]):
		print(item.get('reason', ''))
		sys.exit(0)
print('')
PY
}

mock_bin_dir="$TMP_DIR/mock-bin"
make_gh_mock "$mock_bin_dir"
gh_state_file="$TMP_DIR/gh-state.json"
gh_log_file="$TMP_DIR/gh.log"

# successful block with label normalization and verbose blocked display
repo_success="$TMP_DIR/repo-success"
init_repo "$repo_success"
git -C "$repo_success" checkout -q -b scratch
state_set "$repo_success/subdir" '{"activeIssueNumber":101,"activeIssueTitle":"Issue 101","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/101","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
  "issues": {
    "101": {
      "title": "Issue 101",
      "url": "https://github.com/jmrozi1/ai-dev/issues/101",
      "labels": ["active", "backlog"]
    }
  }
}'
if (
	cd "$repo_success/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" GH_MOCK_LOG="$gh_log_file" "$FLOW" block 'Need upstream validation'
) >"$TMP_DIR/block-success-output" 2>&1; then
	block_success_status=0
else
	block_success_status=$?
fi
block_success_text="$(cat "$TMP_DIR/block-success-output")"
assert_equals "$block_success_status" '0'
assert_contains "$block_success_text" 'Blocked issue 101'
assert_contains "$block_success_text" 'reason: Need upstream validation'
assert_contains "$block_success_text" 'Workflow: inactive'
assert_equals "$(state_get "$repo_success/subdir")" $'{
  "mainBranch": "main",
  "scratchBranch": "scratch",
  "checkpoint": 0
}'
blocked_file_success="$repo_success/.ai-dev/blocked-workflows.json"
assert_file_exists "$blocked_file_success"
blocked_content_success="$(cat "$blocked_file_success")"
assert_contains "$blocked_content_success" '"issueNumber": 101'
assert_contains "$blocked_content_success" '"reason": "Need upstream validation"'
assert_contains "$blocked_content_success" '"blockedAt": "'
assert_equals "$(read_gh_labels_csv "$gh_state_file" '101')" 'blocked'
status_verbose_success="$(run_flow "$repo_success/subdir" status -v)"
assert_contains "$status_verbose_success" 'Blocked workflows:'
assert_contains "$status_verbose_success" 'issue 101 - Issue 101'
assert_contains "$status_verbose_success" 'reason: Need upstream validation'

# successful resume
if (
	cd "$repo_success/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" GH_MOCK_LOG="$gh_log_file" "$FLOW" resume 101
) >"$TMP_DIR/resume-success-output" 2>&1; then
	resume_success_status=0
else
	resume_success_status=$?
fi
resume_success_text="$(cat "$TMP_DIR/resume-success-output")"
assert_equals "$resume_success_status" '0'
assert_contains "$resume_success_text" 'Resumed issue 101'
assert_contains "$resume_success_text" 'checkpoint: 0'
assert_contains "$(state_get "$repo_success/subdir")" '"activeIssueNumber": 101'
assert_equals "$(read_gh_labels_csv "$gh_state_file" '101')" 'active'
assert_equals "$(cat "$blocked_file_success")" $'{
  "blockedWorkflows": []
}'

# missing or blank reason
repo_blank_reason="$TMP_DIR/repo-blank-reason"
init_repo "$repo_blank_reason"
git -C "$repo_blank_reason" checkout -q -b scratch
state_set "$repo_blank_reason/subdir" '{"activeIssueNumber":102,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
blank_reason_output="$TMP_DIR/blank-reason-output"
if run_flow_capture "$repo_blank_reason/subdir" "$blank_reason_output" block ''; then
	blank_reason_status=0
else
	blank_reason_status=$?
fi
assert_equals "$blank_reason_status" '1'
assert_contains "$(cat "$blank_reason_output")" 'Usage: flow block "<reason>"'

# patch workflow rejection
repo_patch="$TMP_DIR/repo-patch"
init_repo "$repo_patch"
git -C "$repo_patch" checkout -q -b scratch
state_set "$repo_patch/subdir" '{"patchDescription":"Local fix","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
patch_output="$TMP_DIR/patch-output"
if run_flow_capture "$repo_patch/subdir" "$patch_output" block 'Cannot continue'; then
	patch_status=0
else
	patch_status=$?
fi
assert_equals "$patch_status" '1'
assert_contains "$(cat "$patch_output")" 'patch workflows are not supported'

# dirty working tree rejection
repo_dirty="$TMP_DIR/repo-dirty"
init_repo "$repo_dirty"
git -C "$repo_dirty" checkout -q -b scratch
state_set "$repo_dirty/subdir" '{"activeIssueNumber":103,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'dirty\n' >> "$repo_dirty/tracked.txt"
dirty_output="$TMP_DIR/dirty-output"
if run_flow_capture "$repo_dirty/subdir" "$dirty_output" block 'Need review'; then
	dirty_status=0
else
	dirty_status=$?
fi
assert_equals "$dirty_status" '1'
assert_contains "$(cat "$dirty_output")" 'repository must be clean'

# scratch and main must be synchronized
repo_diverged="$TMP_DIR/repo-diverged"
init_repo "$repo_diverged"
git -C "$repo_diverged" checkout -q -b scratch
state_set "$repo_diverged/subdir" '{"activeIssueNumber":104,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
printf 'ahead\n' > "$repo_diverged/ahead.txt"
git -C "$repo_diverged" add ahead.txt
git -C "$repo_diverged" commit -q -m 'ahead'
diverged_output="$TMP_DIR/diverged-output"
if run_flow_capture "$repo_diverged/subdir" "$diverged_output" block 'Waiting'; then
	diverged_status=0
else
	diverged_status=$?
fi
assert_equals "$diverged_status" '1'
assert_contains "$(cat "$diverged_output")" 'must equal main'

# nonzero checkpoint rejection
repo_checkpoint="$TMP_DIR/repo-checkpoint"
init_repo "$repo_checkpoint"
git -C "$repo_checkpoint" checkout -q -b scratch
state_set "$repo_checkpoint/subdir" '{"activeIssueNumber":105,"mainBranch":"main","scratchBranch":"scratch","checkpoint":2}' >/dev/null
checkpoint_output="$TMP_DIR/checkpoint-output"
if run_flow_capture "$repo_checkpoint/subdir" "$checkpoint_output" block 'Reason'; then
	checkpoint_status=0
else
	checkpoint_status=$?
fi
assert_equals "$checkpoint_status" '1'
assert_contains "$(cat "$checkpoint_output")" 'checkpoint must be 0'

# blocking with no active workflow
repo_inactive="$TMP_DIR/repo-inactive"
init_repo "$repo_inactive"
git -C "$repo_inactive" checkout -q -b scratch
inactive_output="$TMP_DIR/inactive-output"
if run_flow_capture "$repo_inactive/subdir" "$inactive_output" block 'Reason'; then
	inactive_status=0
else
	inactive_status=$?
fi
assert_equals "$inactive_status" '1'
assert_contains "$(cat "$inactive_output")" 'no active issue is set'

# resume rejects when another workflow is active
repo_resume_active="$TMP_DIR/repo-resume-active"
init_repo "$repo_resume_active"
git -C "$repo_resume_active" checkout -q -b scratch
state_set "$repo_resume_active/subdir" '{"activeIssueNumber":106,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_blocked_payload "$repo_resume_active" '{
  "blockedWorkflows": [
    {
      "issueNumber": 999,
      "issueTitle": "Blocked",
      "issueUrl": "https://github.com/jmrozi1/ai-dev/issues/999",
      "reason": "waiting",
      "blockedAt": "2026-07-23T00:00:00Z"
    }
  ]
}'
resume_active_output="$TMP_DIR/resume-active-output"
if run_flow_capture "$repo_resume_active/subdir" "$resume_active_output" resume 999; then
	resume_active_status=0
else
	resume_active_status=$?
fi
assert_equals "$resume_active_status" '1'
assert_contains "$(cat "$resume_active_output")" 'active issue 106 is already set'

# missing blocked record
repo_missing_record="$TMP_DIR/repo-missing-record"
init_repo "$repo_missing_record"
git -C "$repo_missing_record" checkout -q -b scratch
missing_record_output="$TMP_DIR/missing-record-output"
if run_flow_capture "$repo_missing_record/subdir" "$missing_record_output" resume 123; then
	missing_record_status=0
else
	missing_record_status=$?
fi
assert_equals "$missing_record_status" '1'
assert_contains "$(cat "$missing_record_output")" 'no blocked record exists for issue 123'

# GitHub failure leaves active workflow intact and removes provisional blocked record
repo_gh_fail="$TMP_DIR/repo-gh-fail"
init_repo "$repo_gh_fail"
git -C "$repo_gh_fail" checkout -q -b scratch
state_set "$repo_gh_fail/subdir" '{"activeIssueNumber":107,"activeIssueTitle":"Issue 107","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/107","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_gh_state "$gh_state_file" '{
  "issues": {
    "107": {
      "title": "Issue 107",
      "url": "https://github.com/jmrozi1/ai-dev/issues/107",
      "labels": ["active"]
    }
  }
}'
gh_fail_output="$TMP_DIR/gh-fail-output"
if (
	cd "$repo_gh_fail/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" GH_MOCK_FAIL_EDIT=1 "$FLOW" block 'Blocked by dependency'
) >"$gh_fail_output" 2>&1; then
	gh_fail_status=0
else
	gh_fail_status=$?
fi
assert_equals "$gh_fail_status" '1'
assert_contains "$(cat "$gh_fail_output")" 'GitHub label reconciliation failed'
assert_contains "$(state_get "$repo_gh_fail/subdir")" '"activeIssueNumber": 107'
blocked_file_gh_fail="$repo_gh_fail/.ai-dev/blocked-workflows.json"
if [[ -f "$blocked_file_gh_fail" ]]; then
	assert_equals "$(cat "$blocked_file_gh_fail")" $'{
  "blockedWorkflows": []
}'
fi

# resume normalizes remote labels from backlog using local blocked record authority
repo_resume_label="$TMP_DIR/repo-resume-label"
init_repo "$repo_resume_label"
git -C "$repo_resume_label" checkout -q -b scratch
write_blocked_payload "$repo_resume_label" '{
  "blockedWorkflows": [
    {
      "issueNumber": 108,
      "issueTitle": "Issue 108",
      "issueUrl": "https://github.com/jmrozi1/ai-dev/issues/108",
      "reason": "need test infra",
      "blockedAt": "2026-07-23T00:00:00Z"
    }
  ]
}'
write_gh_state "$gh_state_file" '{
  "issues": {
    "108": {
      "title": "Issue 108",
      "url": "https://github.com/jmrozi1/ai-dev/issues/108",
      "labels": ["backlog"]
    }
  }
}'
resume_label_output="$TMP_DIR/resume-label-output"
if (
	cd "$repo_resume_label/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" "$FLOW" resume 108
) >"$resume_label_output" 2>&1; then
	resume_label_status=0
else
	resume_label_status=$?
fi
resume_label_text="$(cat "$resume_label_output")"
assert_equals "$resume_label_status" '0'
assert_contains "$resume_label_text" 'Resumed issue 108'
assert_contains "$(state_get "$repo_resume_label/subdir")" '"activeIssueNumber": 108'
assert_equals "$(read_gh_labels_csv "$gh_state_file" '108')" 'active'
assert_equals "$(cat "$repo_resume_label/.ai-dev/blocked-workflows.json")" $'{
  "blockedWorkflows": []
}'

# mutually exclusive labels normalize on resume
repo_resume_normalize="$TMP_DIR/repo-resume-normalize"
init_repo "$repo_resume_normalize"
git -C "$repo_resume_normalize" checkout -q -b scratch
write_blocked_payload "$repo_resume_normalize" '{
  "blockedWorkflows": [
    {
      "issueNumber": 109,
      "issueTitle": "Issue 109",
      "issueUrl": "https://github.com/jmrozi1/ai-dev/issues/109",
      "reason": "need sync",
      "blockedAt": "2026-07-23T00:00:00Z"
    }
  ]
}'
write_gh_state "$gh_state_file" '{
  "issues": {
    "109": {
      "title": "Issue 109",
      "url": "https://github.com/jmrozi1/ai-dev/issues/109",
      "labels": ["blocked", "backlog", "active"]
    }
  }
}'
if (
	cd "$repo_resume_normalize/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" "$FLOW" resume 109
) >"$TMP_DIR/resume-normalize-output" 2>&1; then
	resume_normalize_status=0
else
	resume_normalize_status=$?
fi
assert_equals "$resume_normalize_status" '0'
assert_equals "$(read_gh_labels_csv "$gh_state_file" '109')" 'active'

# resume GitHub reconciliation failure preserves local inactive state and blocked record
repo_resume_gh_fail="$TMP_DIR/repo-resume-gh-fail"
init_repo "$repo_resume_gh_fail"
git -C "$repo_resume_gh_fail" checkout -q -b scratch
write_blocked_payload "$repo_resume_gh_fail" '{
	"blockedWorkflows": [
		{
			"issueNumber": 112,
			"issueTitle": "Issue 112",
			"issueUrl": "https://github.com/jmrozi1/ai-dev/issues/112",
			"reason": "waiting on upstream",
			"blockedAt": "2026-07-23T00:00:00Z"
		}
	]
}'
write_gh_state "$gh_state_file" '{
	"issues": {
		"112": {
			"title": "Issue 112",
			"url": "https://github.com/jmrozi1/ai-dev/issues/112",
			"labels": ["blocked"]
		}
	}
}'
blocked_file_resume_gh_fail="$repo_resume_gh_fail/.ai-dev/blocked-workflows.json"
blocked_before_resume_gh_fail="$(cat "$blocked_file_resume_gh_fail")"
resume_gh_fail_output="$TMP_DIR/resume-gh-fail-output"
if (
	cd "$repo_resume_gh_fail/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" GH_MOCK_FAIL_EDIT=1 "$FLOW" resume 112
) >"$resume_gh_fail_output" 2>&1; then
	resume_gh_fail_status=0
else
	resume_gh_fail_status=$?
fi
resume_gh_fail_text="$(cat "$resume_gh_fail_output")"
assert_equals "$resume_gh_fail_status" '1'
assert_contains "$resume_gh_fail_text" 'GitHub label reconciliation failed'
resume_gh_fail_state="$(state_get "$repo_resume_gh_fail/subdir")"
assert_not_contains "$resume_gh_fail_state" '"activeIssueNumber"'
assert_not_contains "$resume_gh_fail_state" '"patchDescription"'
assert_contains "$resume_gh_fail_state" '"mainBranch": "main"'
assert_contains "$resume_gh_fail_state" '"scratchBranch": "scratch"'
assert_contains "$resume_gh_fail_state" '"checkpoint": 0'
assert_equals "$(cat "$blocked_file_resume_gh_fail")" "$blocked_before_resume_gh_fail"
assert_equals "$(read_gh_labels_csv "$gh_state_file" '112')" 'blocked'

# safe repeated/retry behavior overwrites same blocked record
repo_retry="$TMP_DIR/repo-retry"
init_repo "$repo_retry"
git -C "$repo_retry" checkout -q -b scratch
state_set "$repo_retry/subdir" '{"activeIssueNumber":110,"activeIssueTitle":"Issue 110","activeIssueUrl":"https://github.com/jmrozi1/ai-dev/issues/110","mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
write_blocked_payload "$repo_retry" '{
  "blockedWorkflows": [
    {
      "issueNumber": 110,
      "issueTitle": "Issue 110",
      "issueUrl": "https://github.com/jmrozi1/ai-dev/issues/110",
      "reason": "old reason",
      "blockedAt": "2026-07-23T00:00:00Z"
    }
  ]
}'
write_gh_state "$gh_state_file" '{
  "issues": {
    "110": {
      "title": "Issue 110",
      "url": "https://github.com/jmrozi1/ai-dev/issues/110",
      "labels": ["active"]
    }
  }
}'
if (
	cd "$repo_retry/subdir"
	PATH="$mock_bin_dir:$PATH" GH_MOCK_STATE="$gh_state_file" "$FLOW" block 'new reason'
) >"$TMP_DIR/retry-block-output" 2>&1; then
	retry_block_status=0
else
	retry_block_status=$?
fi
assert_equals "$retry_block_status" '0'
assert_equals "$(read_blocked_reason "$repo_retry/.ai-dev/blocked-workflows.json" '110')" 'new reason'

# gh unavailable is rejected
repo_no_gh="$TMP_DIR/repo-no-gh"
init_repo "$repo_no_gh"
git -C "$repo_no_gh" checkout -q -b scratch
state_set "$repo_no_gh/subdir" '{"activeIssueNumber":111,"mainBranch":"main","scratchBranch":"scratch","checkpoint":0}' >/dev/null
no_gh_bin="$TMP_DIR/no-gh-bin"
mkdir -p "$no_gh_bin"
ln -sf "$(command -v bash)" "$no_gh_bin/bash"
ln -sf "$(command -v basename)" "$no_gh_bin/basename"
ln -sf "$(command -v git)" "$no_gh_bin/git"
ln -sf "$(command -v python3)" "$no_gh_bin/python3"
no_gh_output="$TMP_DIR/no-gh-output"
if (
	cd "$repo_no_gh/subdir"
	PATH="$no_gh_bin" "$FLOW" block 'blocked by policy'
) >"$no_gh_output" 2>&1; then
	no_gh_status=0
else
	no_gh_status=$?
fi
assert_equals "$no_gh_status" '1'
assert_contains "$(cat "$no_gh_output")" 'GitHub CLI (gh) is required for this command.'

printf 'flow block/resume tests passed\n'
