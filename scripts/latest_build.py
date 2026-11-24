#!/usr/bin/env python3
"""Print the highest build number uploaded for a marketing version (0 if none).

Usage:
    latest_build.py <bundle-id> <marketing-version>

Credentials: ASC_KEY_ID, ASC_ISSUER_ID, and ASC_PRIVATE_KEY or ASC_KEY_PATH.
"""
import sys

from asc import Client, build_number, resolve_app_id


def main(argv):
    if len(argv) != 2:
        raise SystemExit(__doc__.strip())
    bundle_id, version = argv
    client = Client.from_env(timeout=300)
    app_id = resolve_app_id(client, bundle_id)
    builds = client.all(
        "/v1/builds",
        {"filter[app]": app_id, "filter[preReleaseVersion.version]": version, "limit": "200"},
    )
    dotted = [b["attributes"]["version"] for b in builds if not b["attributes"]["version"].isdigit()]
    if dotted:
        raise RuntimeError(f"builds {', '.join(dotted)} are not plain integers; asc-next cannot pick the next one")
    print(max((build_number(b) for b in builds), default=0))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except RuntimeError as exc:
        raise SystemExit(f"error: {exc}")
