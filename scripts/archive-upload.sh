#!/usr/bin/env bash
#
# Archive an iOS app, export it with the given ExportOptions.plist, and upload
# it to App Store Connect. Signing is automatic: -allowProvisioningUpdates plus
# the API key let xcodebuild use Apple's cloud-managed distribution certificate,
# so no certificate or profile is stored in secrets (the key needs the Admin
# role, or App Manager with access to cloud-managed certificates).
#
# If the export options set destination=upload, the export uploads; otherwise
# the exported .ipa is uploaded with altool.
#
# Inputs (environment):
#   PROJECT or WORKSPACE   .xcodeproj / .xcworkspace path (exactly one)
#   SCHEME                 scheme to archive
#   EXPORT_OPTIONS         ExportOptions.plist path
#   BUILD_NUMBER           CURRENT_PROJECT_VERSION override
#   MARKETING_VERSION      optional MARKETING_VERSION override
#   CONFIGURATION          build configuration (default Release)
#   BUILD_SETTINGS         optional extra KEY=VALUE settings, one per line
#   OUTPUT_DIR             where the archive and export go
#   ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH
#
# Writes archive-path and ipa-path to $GITHUB_OUTPUT when set, before the
# upload, so a caller can still save the .ipa if the upload fails.

set -euo pipefail

: "${SCHEME:?set SCHEME}"
: "${EXPORT_OPTIONS:?set EXPORT_OPTIONS}"
: "${BUILD_NUMBER:?set BUILD_NUMBER}"
: "${OUTPUT_DIR:?set OUTPUT_DIR}"
: "${ASC_KEY_ID:?set ASC_KEY_ID}"
: "${ASC_ISSUER_ID:?set ASC_ISSUER_ID}"
: "${ASC_KEY_PATH:?set ASC_KEY_PATH}"

if [[ -n "${PROJECT:-}" && -z "${WORKSPACE:-}" ]]; then
	container=(-project "${PROJECT}")
elif [[ -n "${WORKSPACE:-}" && -z "${PROJECT:-}" ]]; then
	container=(-workspace "${WORKSPACE}")
else
	echo "error: set exactly one of PROJECT and WORKSPACE" >&2
	exit 2
fi

auth=(
	-allowProvisioningUpdates
	-authenticationKeyID "${ASC_KEY_ID}"
	-authenticationKeyIssuerID "${ASC_ISSUER_ID}"
	-authenticationKeyPath "${ASC_KEY_PATH}"
)

settings=("CURRENT_PROJECT_VERSION=${BUILD_NUMBER}")
[[ -n "${MARKETING_VERSION:-}" ]] && settings+=("MARKETING_VERSION=${MARKETING_VERSION}")
while IFS= read -r line; do
	[[ -n "${line// /}" ]] && settings+=("${line}")
done <<<"${BUILD_SETTINGS:-}"

archive="${OUTPUT_DIR}/${SCHEME}.xcarchive"
export_dir="${OUTPUT_DIR}/export"

echo "==> archive ${SCHEME} (${CONFIGURATION:-Release}, build ${BUILD_NUMBER}${MARKETING_VERSION:+, version ${MARKETING_VERSION}})"
xcodebuild archive \
	"${container[@]}" \
	-scheme "${SCHEME}" \
	-configuration "${CONFIGURATION:-Release}" \
	-destination generic/platform=iOS \
	-archivePath "${archive}" \
	"${auth[@]}" \
	"${settings[@]}"

echo "==> export"
xcodebuild -exportArchive \
	-archivePath "${archive}" \
	-exportOptionsPlist "${EXPORT_OPTIONS}" \
	-exportPath "${export_dir}" \
	"${auth[@]}"

destination="$(/usr/libexec/PlistBuddy -c 'Print :destination' "${EXPORT_OPTIONS}" 2>/dev/null || echo export)"
ipa=""
if [[ "${destination}" != upload ]]; then
	ipa="$(find "${export_dir}" -maxdepth 1 -name '*.ipa' | head -n 1)"
	[[ -n "${ipa}" ]] || { echo "error: the export produced no .ipa in ${export_dir}" >&2; exit 1; }
fi
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
	echo "archive-path=${archive}" >>"${GITHUB_OUTPUT}"
	echo "ipa-path=${ipa}" >>"${GITHUB_OUTPUT}"
fi

if [[ "${destination}" == upload ]]; then
	echo "==> uploaded by the export (destination=upload)"
else
	echo "==> upload ${ipa}"
	# altool finds the key by name (AuthKey_<id>.p8) in API_PRIVATE_KEYS_DIR.
	API_PRIVATE_KEYS_DIR="$(dirname "${ASC_KEY_PATH}")" xcrun altool --upload-app \
		--type ios \
		--file "${ipa}" \
		--apiKey "${ASC_KEY_ID}" \
		--apiIssuer "${ASC_ISSUER_ID}"
fi

echo "==> build ${BUILD_NUMBER} uploaded"
