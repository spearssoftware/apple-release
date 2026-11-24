#!/usr/bin/env bash
#
# Write the App Store Connect API key from $ASC_PRIVATE_KEY to
# <dir>/AuthKey_<key-id>.p8 (the name altool looks for) and print the path.
# Accepts the raw .p8 contents or the same contents base64-encoded, since repos
# store the secret both ways.
#
# Usage: ASC_PRIVATE_KEY=... write-key.sh <key-id> <dir>

set -euo pipefail

key_id="${1:?usage: write-key.sh <key-id> <dir>}"
dir="${2:?usage: write-key.sh <key-id> <dir>}"
: "${ASC_PRIVATE_KEY:?set ASC_PRIVATE_KEY to the .p8 contents (raw or base64)}"

path="${dir}/AuthKey_${key_id}.p8"
umask 077
mkdir -p "${dir}"
if [[ "${ASC_PRIVATE_KEY}" == *"-----BEGIN"* ]]; then
	printf '%s\n' "${ASC_PRIVATE_KEY}" >"${path}"
else
	printf '%s' "${ASC_PRIVATE_KEY}" | base64 --decode >"${path}" 2>/dev/null || true
fi

if ! grep -q -- '-----BEGIN PRIVATE KEY-----' "${path}"; then
	rm -f "${path}"
	echo "error: the App Store Connect private key is neither a .p8 nor a base64-encoded .p8" >&2
	exit 1
fi
echo "${path}"
