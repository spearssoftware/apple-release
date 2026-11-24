#!/usr/bin/env bash
#
# Select the newest release (non-beta) Xcode whose version starts with the
# given prefix, e.g. "26" or "27.0", so a job survives runner image updates
# that add or drop point releases.
#
# Usage: select-xcode.sh <version-prefix>

set -euo pipefail

prefix="${1:?usage: select-xcode.sh <version-prefix>}"
xcode="$(find /Applications -maxdepth 1 \( -name "Xcode_${prefix}.app" -o -name "Xcode_${prefix}.*.app" \) ! -iname '*beta*' | sort -V | tail -n 1)"
if [[ -z "${xcode}" ]]; then
	echo "error: no release Xcode ${prefix} on this runner. Installed:" >&2
	find /Applications -maxdepth 1 -name 'Xcode*.app' >&2
	exit 1
fi
sudo xcode-select -s "${xcode}"
xcodebuild -version
