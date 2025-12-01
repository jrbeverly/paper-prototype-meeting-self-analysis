#!/usr/bin/env bash
set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
requested_output="${1:-dist/meeting-intelligence-source.zip}"

if [[ "${requested_output}" = /* ]]; then
  output_path="${requested_output}"
else
  output_path="${repository_root}/${requested_output}"
fi

mkdir -p "$(dirname "${output_path}")"

bundle_directory="$(mktemp -d)"
trap 'rm -rf "${bundle_directory}"' EXIT
temporary_bundle="${bundle_directory}/meeting-intelligence-source.zip"

files=()
while IFS= read -r -d '' path; do
  [[ -f "${repository_root}/${path}" ]] || continue
  case "${path}" in
    buildspecs/* | cmd/* | meeting_intelligence/* | schemas/* | templates/* | go.mod | go.sum | pyproject.toml)
      files+=("${path}")
      ;;
  esac
done < <(
  git -C "${repository_root}" \
    ls-files --cached --others --exclude-standard -z
)

if [[ "${#files[@]}" -eq 0 ]]; then
  echo "No source files found." >&2
  exit 1
fi

(
  cd "${repository_root}"
  zip -q "${temporary_bundle}" "${files[@]}"
)

mv "${temporary_bundle}" "${output_path}"
echo "Created ${output_path} with ${#files[@]} files."
