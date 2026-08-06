#!/usr/bin/env bash
set -euo pipefail

force=false

show_help() {
	cat <<'EOF'
Usage: scripts/install-skills.sh [-f|--force]

Install repository skills into ~/.agents/skills.

Options:
  -f, --force  Replace matching installed skills without prompting.
  -h, --help   Show this help and exit.

Unrelated skills already installed under ~/.agents/skills are left unchanged.
EOF
}

while (($# > 0)); do
	case "$1" in
		-f|--force)
			force=true
			;;
		-h|--help)
			show_help
			exit 0
			;;
		*)
			printf 'install-skills.sh: unknown option: %s\n' "$1" >&2
			show_help >&2
			exit 2
			;;
	esac
	shift
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
source_root="$repo_root/skills"
destination_root="${HOME}/.agents/skills"

if [[ ! -d "$source_root" ]]; then
	printf 'install-skills.sh: skills directory not found: %s\n' "$source_root" >&2
	exit 1
fi

mkdir -p "$destination_root"

installed=0
skipped=0

while IFS= read -r -d '' skill_file; do
	source_dir="$(dirname "$skill_file")"
	relative_path="${source_dir#"$source_root"/}"
	destination_dir="$destination_root/$relative_path"

	if [[ -e "$destination_dir" && "$force" != true ]]; then
		if [[ ! -t 0 ]]; then
			printf 'Skipping existing skill without an interactive terminal: %s (use -f to replace)\n' "$relative_path" >&2
			((skipped += 1))
			continue
		fi

		read -r -p "Replace existing skill '$relative_path'? [y/N] " response
		case "$response" in
			y|Y|yes|YES|Yes)
				;;
			*)
				printf 'Skipped %s\n' "$relative_path"
				((skipped += 1))
				continue
				;;
		esac
	fi

	rm -rf "$destination_dir"
	mkdir -p "$(dirname "$destination_dir")"
	cp -R "$source_dir" "$destination_dir"
	printf 'Installed %s\n' "$relative_path"
	((installed += 1))
done < <(find "$source_root" -type f -name SKILL.md -print0 | sort -z)

printf 'Skill installation complete: %d installed, %d skipped.\n' "$installed" "$skipped"
