#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_SUITE="unit"

unit_modules=(
	tests.test_script_entrypoints
	tests.test_bootstrap
	tests.test_bootstrap_cli
)

bootstrap_modules=(
	tests.test_bootstrap
	tests.test_bootstrap_cli
)

integration_discovery_args=(
	discover
	-s
	tests
	-p
	test_*.py
)

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
flow_shell_dir_default="$repo_root/tests/shell/flow"
bootstrap_shell_dir_default="$repo_root/tests/shell/bootstrap"

flow_shell_dir="${AI_DEV_TEST_FLOW_DIR:-$flow_shell_dir_default}"
bootstrap_shell_dir="${AI_DEV_TEST_BOOTSTRAP_DIR:-$bootstrap_shell_dir_default}"

source "$repo_root/tools/bootstrap/python_select.sh"

discover_shell_tests() {
	local directory="$1"
	local -n discovered_ref="$2"
	discovered_ref=()

	if [[ ! -d "$directory" ]]; then
		return 0
	fi

	while IFS= read -r -d '' file_path; do
		discovered_ref+=("$file_path")
	done < <(find "$directory" -maxdepth 1 -type f -name 'test-*.sh' -print0 | sort -z)
}

print_shell_listing() {
	local suite_name="$1"
	local directory="$2"
	local tests_var_name="$3"
	local -n tests_ref="$tests_var_name"

	printf '%s:\n' "$suite_name"
	printf '  purpose: %s\n' "Run shell suites discovered under $directory"
	printf '  shell-dir: %s\n' "$directory"
	if ((${#tests_ref[@]} == 0)); then
		printf '  shell-tests: none discovered\n'
		return
	fi
	printf '  shell-tests:\n'
	local shell_test
	for shell_test in "${tests_ref[@]}"; do
		if [[ "$shell_test" == "$repo_root"/* ]]; then
			printf '    - %s\n' "${shell_test#"$repo_root"/}"
		else
			printf '    - %s\n' "$shell_test"
		fi
	done
}

show_help() {
	cat <<'EOF'
Usage: scripts/test.sh [suite] [options] [-- unittest-args]

Run repository test suites through one canonical dispatcher.

Suites:
  unit         Fast Python unit coverage (default).
  bootstrap    Bootstrap-focused Python tests and shell bootstrap suites.
  flow         Shell lifecycle suites under tests/shell/flow/.
  integration  Broader cross-component Python discovery suite.
  all          Complete Python + shell matrix.

Options:
  -h, --help   Show this help and exit.
  --list       Show suite mapping and underlying tests/directories.
  --all        Alias for suite "all".

Default behavior:
  Runs the "unit" suite:
    tests.test_script_entrypoints
    tests.test_bootstrap
    tests.test_bootstrap_cli

Forwarded unittest args:
  Use -- to pass arguments to Python unittest invocations.
  Supported suites for forwarded unittest args: unit, bootstrap, integration, all.
  The flow suite rejects forwarded unittest args because it only runs shell suites.

Minimum Python version: 3.8

Examples:
  scripts/test.sh
  scripts/test.sh bootstrap
  scripts/test.sh integration -- -k review
  scripts/test.sh all -- -k namespace
EOF
}

show_list() {
	local -a flow_shell_tests=()
	local -a bootstrap_shell_tests=()
	discover_shell_tests "$flow_shell_dir" flow_shell_tests
	discover_shell_tests "$bootstrap_shell_dir" bootstrap_shell_tests

	printf 'unit:\n'
	printf '  purpose: Fast Python unit coverage without shell lifecycle suites\n'
	printf '  python-modules:\n'
	printf '    - %s\n' "${unit_modules[@]}"

	printf 'bootstrap:\n'
	printf '  purpose: Bootstrap-focused Python and shell bootstrap coverage\n'
	printf '  python-modules:\n'
	printf '    - %s\n' "${bootstrap_modules[@]}"
	print_shell_listing "  bootstrap-shell" "$bootstrap_shell_dir" bootstrap_shell_tests

	print_shell_listing "flow" "$flow_shell_dir" flow_shell_tests

	printf 'integration:\n'
	printf '  purpose: Broader cross-component Python integration discovery suite\n'
	printf '  python-discovery: python -m unittest discover -s tests -p test_*.py\n'

	printf 'all:\n'
	printf '  purpose: Complete Python and shell matrix (integration + bootstrap shell + flow shell)\n'
	printf '  python-discovery: python -m unittest discover -s tests -p test_*.py\n'
	print_shell_listing "  all-bootstrap-shell" "$bootstrap_shell_dir" bootstrap_shell_tests
	print_shell_listing "  all-flow-shell" "$flow_shell_dir" flow_shell_tests
}

run_python_modules() {
	local python_executable="$1"
	shift
	local -a modules=("$@")
	"$python_executable" -m unittest "${modules[@]}" "${forward_args[@]}"
}

run_python_discovery() {
	local python_executable="$1"
	"$python_executable" -m unittest "${integration_discovery_args[@]}" "${forward_args[@]}"
}

run_shell_suite() {
	local suite_label="$1"
	local directory="$2"
	local -a discovered=()
	discover_shell_tests "$directory" discovered

	if ((${#discovered[@]} == 0)); then
		printf '%s: no shell tests discovered under %s\n' "$suite_label" "$directory"
		return 0
	fi

	local shell_test
	for shell_test in "${discovered[@]}"; do
		if [[ "$shell_test" == "$repo_root"/* ]]; then
			printf '[%s] bash %s\n' "$suite_label" "${shell_test#"$repo_root"/}"
		else
			printf '[%s] bash %s\n' "$suite_label" "$shell_test"
		fi
		bash "$shell_test"
	done
}

suite=""
forward_args=()

while (($#)); do
	case "$1" in
		-h|--help)
			show_help
			exit 0
			;;
		--list)
			show_list
			exit 0
			;;
		--all)
			if [[ -n "$suite" && "$suite" != "all" ]]; then
				printf 'test.sh: multiple suites specified (%s and all).\n' "$suite" >&2
				show_help >&2
				exit 2
			fi
			suite="all"
			shift
			;;
		--)
			shift
			forward_args=("$@")
			break
			;;
		-*)
			printf 'test.sh: unsupported option: %s\n' "$1" >&2
			show_help >&2
			exit 2
			;;
		*)
			if [[ -n "$suite" ]]; then
				printf 'test.sh: multiple suites specified (%s and %s).\n' "$suite" "$1" >&2
				show_help >&2
				exit 2
			fi
			suite="$1"
			shift
			;;
	esac
done

if [[ -z "$suite" ]]; then
	suite="$DEFAULT_SUITE"
fi

case "$suite" in
	unit|bootstrap|flow|integration|all)
		;;
	*)
		printf 'test.sh: unknown suite: %s\n' "$suite" >&2
		show_help >&2
		exit 2
		;;
esac

if [[ "$suite" == "flow" && ${#forward_args[@]} -gt 0 ]]; then
	printf 'test.sh: suite "flow" does not accept unittest args after --.\n' >&2
	show_help >&2
	exit 2
fi

python_executable=""
if [[ "$suite" != "flow" ]]; then
	python_executable="$(ai_dev_select_python "test.sh")" || exit 1
fi

case "$suite" in
	unit)
		exec "$python_executable" -m unittest "${unit_modules[@]}" "${forward_args[@]}"
		;;
	bootstrap)
		run_python_modules "$python_executable" "${bootstrap_modules[@]}"
		run_shell_suite "bootstrap" "$bootstrap_shell_dir"
		;;
	flow)
		run_shell_suite "flow" "$flow_shell_dir"
		;;
	integration)
		exec "$python_executable" -m unittest "${integration_discovery_args[@]}" "${forward_args[@]}"
		;;
	all)
		run_python_discovery "$python_executable"
		run_shell_suite "bootstrap" "$bootstrap_shell_dir"
		run_shell_suite "flow" "$flow_shell_dir"
		;;
esac

exit 0
