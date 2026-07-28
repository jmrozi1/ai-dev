#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

errors=0

pass() {
  printf '[PASS] %s\n' "$1"
}

fail() {
  printf '[FAIL] %s\n' "$1"
  errors=$((errors + 1))
}

check_directory_exists() {
  local dir="$1"
  if [[ -d "$dir" ]]; then
    pass "Directory exists: $dir"
  else
    fail "Missing directory: $dir"
  fi
}

check_readme_exists() {
  local file="$1"
  if [[ -f "$file" ]]; then
    pass "Placeholder README exists: $file"
  else
    fail "Missing placeholder README: $file"
  fi
}

check_no_tracked_matches() {
  local label="$1"
  local regex="$2"
  local -a matches=()

  for tracked_file in "${TRACKED_FILES[@]}"; do
    if [[ "$tracked_file" =~ $regex ]]; then
      matches+=("$tracked_file")
    fi
  done

  if [[ ${#matches[@]} -eq 0 ]]; then
    pass "$label"
  else
    fail "$label"
    printf '       Tracked files that should be ignored:\n'
    printf '       - %s\n' "${matches[@]}"
  fi
}

if ! command -v git >/dev/null 2>&1; then
  echo 'git is required to run this validation.' >&2
  exit 1
fi

if [[ ! -d .git ]]; then
  echo 'Run this script from inside a git repository checkout.' >&2
  exit 1
fi

mapfile -t TRACKED_FILES < <(git ls-files --cached --others --exclude-standard)

echo 'Validating Jenkins platform repository scaffold...'

EXPECTED_TOP_LEVEL_DIRS=(
  compose
  docs
  environments
  image-definitions
  jcasc
  job-dsl
  scripts
  shared-library
  tests
)

for dir in "${EXPECTED_TOP_LEVEL_DIRS[@]}"; do
  check_directory_exists "$dir"
done

PLACEHOLDER_READMES=(
  README.md
  compose/README.md
  docs/README.md
  environments/README.md
  environments/dev/README.md
  image-definitions/README.md
  jcasc/README.md
  job-dsl/README.md
  scripts/README.md
  shared-library/README.md
  tests/README.md
)

for readme_file in "${PLACEHOLDER_READMES[@]}"; do
  check_readme_exists "$readme_file"
done

check_no_tracked_matches \
  'Credentials and secret files are not tracked' \
  '(^|/)\.env(\..*)?$|\.local$|\.override$|\.(secret|secrets|key|pem|p12|jks)$|(^|/)secrets(/|$)'

check_no_tracked_matches \
  'Jenkins runtime data and generated state are not tracked' \
  '(^|/)(jenkins_home|jenkins-data|\.jenkins|compose-data|volumes|\.docker)(/|$)|(^|/)(logs|tmp|temp|build|dist|out)(/|$)|\.log$'

check_no_tracked_matches \
  'Generated review output out.txt is not tracked' \
  '^out\.txt$'

echo
if [[ $errors -eq 0 ]]; then
  echo 'Validation succeeded.'
  exit 0
fi

echo "Validation failed with $errors issue(s)."
exit 1
