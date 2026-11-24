# apple-release

Composite GitHub Actions for shipping spearssoftware iOS apps to TestFlight.
Each app keeps its own workflow and its own preparation steps (XcodeGen, Tauri,
Capacitor, asset downloads) and calls these actions for the parts every app
shares.

| Action | Runner | What it does |
| --- | --- | --- |
| `spearssoftware/apple-release/build-number@v1` | any | Picks the CFBundleVersion: `epoch-minutes` (default) or `asc-next` (highest build of the marketing version in App Store Connect + 1). |
| `spearssoftware/apple-release/archive-upload@v1` | macOS | Selects Xcode, archives, exports and uploads with automatic, cloud-managed signing. |
| `spearssoftware/apple-release/notes@v1` | Linux | Waits for processing, then fills the build's "What to Test" with `<branch> @ <sha>  (build <n>)` and recent commit subjects. |

The repository is public because public app repos (BibleMarker) cannot use
actions from a private one. It holds no secrets: callers pass the App Store
Connect key as inputs.

## Credentials

Every action that talks to App Store Connect takes `key-id`, `issuer-id` and
`private-key`. `private-key` accepts the `.p8` contents raw (the org secret
`ASC_PRIVATE_KEY`) or base64-encoded (older per-repo `APP_STORE_CONNECT_API_KEY`
secrets). The key needs the Admin role, or App Manager with access to
cloud-managed distribution certificates, since no certificate is stored.

The org secrets `ASC_KEY_ID`, `ASC_ISSUER_ID` and `ASC_PRIVATE_KEY` are visible
to private repositories only; a public app repo uses its own repository secrets.

## Usage

Run only the archive on macOS (billed at 10x on private repos) and do the
processing wait on Linux:

```yaml
jobs:
  build:
    runs-on: macos-15
    outputs:
      build: ${{ steps.number.outputs.build }}
      sha: ${{ steps.sha.outputs.sha }}
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ inputs.ref }}
      - id: sha
        run: echo "sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"
      - id: number
        uses: spearssoftware/apple-release/build-number@v1
      # ... app-specific preparation ...
      - uses: spearssoftware/apple-release/archive-upload@v1
        with:
          project: ios/App/App.xcodeproj
          scheme: App
          export-options: ios/ExportOptions.plist
          build-number: ${{ steps.number.outputs.build }}
          xcode: "26"
          key-id: ${{ secrets.ASC_KEY_ID }}
          issuer-id: ${{ secrets.ASC_ISSUER_ID }}
          private-key: ${{ secrets.ASC_PRIVATE_KEY }}

  notes:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ needs.build.outputs.sha }}
          fetch-depth: 50
      - uses: spearssoftware/apple-release/notes@v1
        with:
          bundle-id: com.example.app
          build-number: ${{ needs.build.outputs.build }}
          branch: ${{ inputs.ref }}
          key-id: ${{ secrets.ASC_KEY_ID }}
          issuer-id: ${{ secrets.ASC_ISSUER_ID }}
          private-key: ${{ secrets.ASC_PRIVATE_KEY }}
```

With `asc-next` numbering, pass `marketing-version` to `notes` too (numbers
restart per version) and give the workflow a `concurrency` group so two runs
cannot pick the same number.

To rewrite notes for a build already in TestFlight, run the `notes` action on
its own with the build's number (or `newest: "true"`), from a checkout of the
commit that was built.

## Versioning

Callers pin `@v1`. Releases are tagged `v1.x.y`, and the `v1` tag is moved to
the newest compatible release. A breaking input change gets `v2`.

## Development

```bash
just check
```
