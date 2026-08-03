#!/usr/bin/env bash

AI_DEV_MIN_PYTHON_MAJOR=3
AI_DEV_MIN_PYTHON_MINOR=8
AI_DEV_MIN_PYTHON_PATCH=0

ai_dev_min_python_version() {
	printf '%s.%s.%s' "$AI_DEV_MIN_PYTHON_MAJOR" "$AI_DEV_MIN_PYTHON_MINOR" "$AI_DEV_MIN_PYTHON_PATCH"
}

ai_dev_resolve_python_candidate() {
	local candidate="$1"

	if [[ -z "$candidate" ]]; then
		return 0
	fi

	if [[ "$candidate" == */* ]]; then
		if [[ -x "$candidate" ]]; then
			printf '%s' "$candidate"
			return 0
		fi
		return 0
	fi

	command -v -- "$candidate" 2>/dev/null || true
}

ai_dev_probe_python_version() {
	local python_path="$1"
	local output

	if ! output="$("$python_path" -c 'import sys; print("{}.{}.{}".format(*sys.version_info[:3]))' 2>&1)"; then
		output=${output//$'\n'/ }
		printf 'PROBE_FAILED:%s' "$output"
		return 1
	fi

	output=${output//$'\r'/}
	output=${output%%$'\n'*}
	printf '%s' "$output"
}

ai_dev_python_version_is_compatible() {
	local version="$1"
	local major minor patch

	IFS=. read -r major minor patch <<EOF
$version
EOF

	if [[ -z "${major:-}" || -z "${minor:-}" ]]; then
		return 1
	fi

	if ! [[ "$major" =~ ^[0-9]+$ && "$minor" =~ ^[0-9]+$ ]]; then
		return 1
	fi

	if (( major > AI_DEV_MIN_PYTHON_MAJOR )); then
		return 0
	fi
	if (( major < AI_DEV_MIN_PYTHON_MAJOR )); then
		return 1
	fi
	if (( minor < AI_DEV_MIN_PYTHON_MINOR )); then
		return 1
	fi
	return 0
}

ai_dev_select_python() {
	local caller_name="$1"
	local minimum_version
	minimum_version="$(ai_dev_min_python_version)"
	local -a rejected=()
	local seen_paths='|'
	local candidate resolved version
	local selected_python=""

	_add_rejected() {
		rejected+=("$1")
	}

	_seen_path() {
		local path="$1"
		[[ "$seen_paths" == *"|$path|"* ]]
	}

	_mark_seen() {
		local path="$1"
		seen_paths+="$path|"
	}

	_try_candidate() {
		local label="$1"
		local path="$2"
		local explicit="$3"
		selected_python=""

		if [[ -z "$path" ]]; then
			if [[ "$explicit" == "1" ]]; then
				printf '%s: AI_DEV_PYTHON is set but was not found or not executable: %s\n' "$caller_name" "${AI_DEV_PYTHON}" >&2
				printf '%s: AI_DEV_PYTHON must point to Python >= %s.\n' "$caller_name" "$minimum_version" >&2
				return 2
			fi
			return 1
		fi

		if _seen_path "$path"; then
			return 1
		fi
		_mark_seen "$path"

		if ! version="$(ai_dev_probe_python_version "$path")"; then
			_add_rejected "$label -> $path ($version)"
			if [[ "$explicit" == "1" ]]; then
				printf '%s: AI_DEV_PYTHON is set but could not be validated: %s\n' "$caller_name" "${AI_DEV_PYTHON}" >&2
				printf '%s: %s\n' "$caller_name" "$label -> $path ($version)" >&2
				printf '%s: AI_DEV_PYTHON must point to Python >= %s.\n' "$caller_name" "$minimum_version" >&2
				return 2
			fi
			return 1
		fi

		if ai_dev_python_version_is_compatible "$version"; then
			selected_python="$path"
			return 0
		fi

		_add_rejected "$label -> $path (version $version)"
		if [[ "$explicit" == "1" ]]; then
			printf '%s: AI_DEV_PYTHON is set to an incompatible interpreter: %s (version %s).\n' "$caller_name" "$path" "$version" >&2
			printf '%s: Minimum supported Python version is %s.\n' "$caller_name" "$minimum_version" >&2
			return 2
		fi

		return 1
	}

	if [[ -n "${AI_DEV_PYTHON:-}" ]]; then
		resolved="$(ai_dev_resolve_python_candidate "$AI_DEV_PYTHON")"
		if _try_candidate "AI_DEV_PYTHON=$AI_DEV_PYTHON" "$resolved" "1"; then
			printf '%s' "$selected_python"
			return 0
		fi
		return 1
	fi

	for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
		resolved="$(ai_dev_resolve_python_candidate "$candidate")"
		if _try_candidate "$candidate" "$resolved" "0"; then
			printf '%s' "$selected_python"
			return 0
		fi
	done

	resolved="$(ai_dev_resolve_python_candidate "python")"
	if _try_candidate "python" "$resolved" "0"; then
		printf '%s' "$selected_python"
		return 0
	fi

	printf '%s: No compatible Python interpreter found. Minimum supported version is %s.\n' "$caller_name" "$minimum_version" >&2
	if ((${#rejected[@]})); then
		printf '%s: Discovered but rejected interpreters:\n' "$caller_name" >&2
		for detail in "${rejected[@]}"; do
			printf '  - %s\n' "$detail" >&2
		done
	fi
	printf '%s: Set AI_DEV_PYTHON to a compatible interpreter path to continue.\n' "$caller_name" >&2
	return 1
}
