#!/usr/bin/env python3
r"""Write TestFlight "What to Test" notes for an uploaded build.

CFBundleVersion is limited to period-separated integers, so the build number
can't carry a SHA or a branch name. The beta build localization notes are the
only place that provenance can live. This script waits for the build to appear
and finish processing, writes the notes from the current checkout, and reads
them back.

Usage, from a checkout of the commit that was built:

    testflight_notes.py --bundle-id com.example.app --build-number 42 --branch main

Retry path, when a notes write failed but the build is already in TestFlight
(--newest targets the most recently uploaded build):

    testflight_notes.py --bundle-id com.example.app --newest

Pass --marketing-version when build numbers repeat across versions (a
"highest + 1" scheme restarts per version); leave it off for globally unique
numbers such as epoch minutes, where a version that drifted from the archive's
would silently match nothing.

Credentials come from ASC_KEY_ID, ASC_ISSUER_ID and ASC_KEY_PATH.
"""
import argparse
import subprocess
import time

from asc import Client, log, resolve_app_id

# App Store Connect caps beta build localization notes at 4000 characters.
NOTES_MAX = 4000

# Grace period granted to the write/verify calls once polling is done, so a
# nearly-exhausted poll deadline doesn't leave them with no retries.
WRITE_GRACE = 120

# Terminal states for Build.processingState. Notes are rejected while a build is
# still PROCESSING, so we wait for one of these before writing.
DONE_STATES = {"VALID", "FAILED", "INVALID"}


def find_build(client, app_id, build_number, marketing_version=None):
    """Look a build up by CFBundleVersion, or newest-first when unspecified.

    Apple only guarantees CFBundleVersion uniqueness within a marketing version,
    so ask for two and sort, to notice ambiguity rather than pick blind.
    """
    params = {"filter[app]": app_id, "sort": "-uploadedDate", "limit": "2"}
    if build_number:
        params["filter[version]"] = build_number
    if marketing_version:
        params["filter[preReleaseVersion.version]"] = marketing_version

    data = client.request("GET", "/v1/builds", params)["data"]
    if build_number and len(data) > 1:
        log(
            f"  warning: {len(data)} builds match version {build_number}; "
            "using the most recently uploaded (pass --marketing-version to narrow)"
        )
    return data[0] if data else None


def wait_for_build(client, app_id, build_number, marketing_version=None):
    """Wait for the build to appear, then for it to finish processing.

    Both waits share client.deadline. A freshly uploaded build is not queryable
    for several minutes after the upload command exits, so "not found" is an
    expected transient state here, not an error.
    """
    target = build_number or "most recent"

    while True:
        build = find_build(client, app_id, build_number, marketing_version)
        if build:
            break
        if time.monotonic() >= client.deadline:
            raise RuntimeError(f"build {target} never appeared before the deadline")
        log(f"  build {target} not visible yet; waiting {client.interval}s")
        time.sleep(client.interval)

    while True:
        attrs = build["attributes"]
        state = attrs["processingState"]
        if state in DONE_STATES:
            break
        if time.monotonic() >= client.deadline:
            raise RuntimeError(f"build {attrs['version']} still {state} at the deadline")
        log(f"  build {attrs['version']} is {state}; waiting {client.interval}s")
        time.sleep(client.interval)
        build = client.request("GET", f"/v1/builds/{build['id']}")["data"]

    state = build["attributes"]["processingState"]
    if state != "VALID":
        raise RuntimeError(f"build {build['attributes']['version']} is {state}")

    return build


def localizations(client, build_id):
    return client.request("GET", f"/v1/builds/{build_id}/betaBuildLocalizations")["data"]


def find_en_us(locs):
    return next((loc for loc in locs if loc["attributes"].get("locale") == "en-US"), None)


def write_notes(client, build_id, notes):
    """PATCH the existing en-US localization, or POST one if there isn't one."""
    existing = localizations(client, build_id)

    # The attribute is `whatsNew`, NOT `whatsToTest`: TestFlight's UI labels the
    # field "What to Test", but sending `whatsToTest` fails with
    # ENTITY_ERROR.ATTRIBUTE.UNKNOWN. The attribute keys are logged here and in
    # verify_notes so the live field name stays checkable from the build log.
    for loc in existing:
        log(
            f"  existing localization {loc['attributes'].get('locale')}: "
            f"attributes {sorted(loc['attributes'])}"
        )

    en_us = find_en_us(existing)
    if en_us:
        client.request(
            "PATCH",
            f"/v1/betaBuildLocalizations/{en_us['id']}",
            body={
                "data": {
                    "type": "betaBuildLocalizations",
                    "id": en_us["id"],
                    "attributes": {"whatsNew": notes},
                }
            },
        )
        log(f"  patched existing en-US localization {en_us['id']}")
        return

    client.request(
        "POST",
        "/v1/betaBuildLocalizations",
        body={
            "data": {
                "type": "betaBuildLocalizations",
                "attributes": {"locale": "en-US", "whatsNew": notes},
                "relationships": {"build": {"data": {"type": "builds", "id": build_id}}},
            }
        },
    )
    log("  created en-US localization")


def normalize(text):
    return (text or "").replace("\r\n", "\n").strip()


def verify_notes(client, build_id, expected, attempts=3, wait=5):
    """Read the notes back; a 200 on the write is not proof they landed.

    Retried a few times: the relationship endpoint can briefly serve the
    pre-write representation, which would otherwise look like a failed write.
    """
    actual = ""
    for attempt in range(1, attempts + 1):
        en_us = find_en_us(localizations(client, build_id))
        if en_us:
            log(f"  en-US localization attributes: {sorted(en_us['attributes'])}")
            actual = en_us["attributes"].get("whatsNew")
            if normalize(actual) == normalize(expected):
                return
        if attempt < attempts:
            log(f"  notes not readable back yet (attempt {attempt}); waiting {wait}s")
            time.sleep(wait)

    raise RuntimeError(
        "notes read back did not match what was written:\n"
        f"--- expected ---\n{expected}\n--- actual ---\n{actual}"
    )


def git(*args):
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def compose_notes(branch, build_number, count):
    """Build the notes body from the current checkout.

    The header format `<branch> @ <sha>  (build <n>)` is load-bearing: callers
    identify release builds by its prefix (BibleMarker's store submission looks
    for `app-v<version> @ `), so change it only together with them.
    """
    # Without a SHA there is no provenance to write, only a header naming a
    # branch, which would overwrite good notes on a rebuild with worse ones.
    sha = git("rev-parse", "--short", "HEAD")
    if not sha:
        raise RuntimeError("cannot resolve HEAD; the notes must be composed from a git checkout")

    branch = branch or git("rev-parse", "--abbrev-ref", "HEAD") or "unknown"

    header = f"{branch} @ {sha}"
    if build_number:
        header += f"  (build {build_number})"

    lines = [header]
    subjects = git("log", f"-{count}", "--no-merges", "--pretty=format:- %s")
    if subjects:
        lines += ["", "Recent commits:", subjects]

    return "\n".join(lines)


def truncate(notes):
    if len(notes) <= NOTES_MAX:
        return notes
    log(f"  notes are {len(notes)} chars; truncating to {NOTES_MAX}")
    return notes[: NOTES_MAX - 4].rstrip() + "\n..."


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--build-number", help="CFBundleVersion to target.")
    parser.add_argument(
        "--newest",
        action="store_true",
        help="Target the most recently uploaded build instead of a build number. Only for the retry path.",
    )
    parser.add_argument("--marketing-version", help="Only match builds of this CFBundleShortVersionString.")
    parser.add_argument(
        "--branch",
        help="Branch/tag label for the header. The SHA and commit list come "
        "from the checkout, so check out the commit that was built.",
    )
    parser.add_argument("--commit-count", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args(argv)

    # An empty --build-number must not silently fall through to "newest": that
    # would stamp this run's provenance onto an unrelated build.
    if args.newest:
        if args.build_number:
            parser.error("--newest and --build-number are mutually exclusive")
    elif not args.build_number:
        parser.error("--build-number is required (or pass --newest)")

    return args


def main(argv=None):
    args = parse_args(argv)

    notes = truncate(compose_notes(args.branch, args.build_number, args.commit_count).strip())
    log("Notes to write:")
    log("\n".join("  | " + line for line in notes.splitlines()))

    client = Client.from_env(timeout=args.timeout, interval=args.interval)

    app_id = resolve_app_id(client, args.bundle_id)
    log(f"Resolved {args.bundle_id} to app {app_id}")

    build = wait_for_build(client, app_id, args.build_number, args.marketing_version)
    attrs = build["attributes"]
    log(f"Build {attrs['version']} ({build['id']}) is VALID, uploaded {attrs.get('uploadedDate')}")

    client.budget(WRITE_GRACE)
    write_notes(client, build["id"], notes)
    verify_notes(client, build["id"], notes)
    log("Notes verified by reading them back from the API.")


if __name__ == "__main__":
    # Deadline and API failures are expected outcomes, not crashes: report them
    # the same way as the precondition checks rather than as a traceback.
    try:
        main()
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}")
