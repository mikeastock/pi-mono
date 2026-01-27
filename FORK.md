# Fork Versioning

This is a fork of [badlogic/pi-mono](https://github.com/badlogic/pi-mono). The following packages are published under the `@mikeastock` scope:

- `@mikeastock/pi-mom` (upstream: `@mariozechner/pi-mom`)

## Versioning Scheme

Versions use semver pre-release tags to encode the upstream base version:

```
{upstream_version}-fork.{n}
```

Examples: `0.50.1-fork.1`, `0.50.1-fork.2`, `0.51.0-fork.1`

The upstream version portion makes it immediately clear which release the fork is based on. The fork number (`n`) increments with each fork-specific publish.

## Release Script

All fork versioning is handled by `scripts/fork-release.mjs`:

```bash
node scripts/fork-release.mjs status            # Show current version and upstream info
node scripts/fork-release.mjs bump              # 0.50.1-fork.1 → 0.50.1-fork.2
node scripts/fork-release.mjs rebase            # Auto-detect latest upstream tag, reset to fork.1
node scripts/fork-release.mjs rebase 0.51.0     # Explicit upstream version
node scripts/fork-release.mjs publish           # npm publish --access public --tag fork
```

The `--tag fork` dist-tag lets users install with:

```bash
npm install @mikeastock/pi-mom@fork
```

## Workflow

| Event | Command |
|---|---|
| Fork-specific changes | `node scripts/fork-release.mjs bump` |
| Rebase onto new upstream | `node scripts/fork-release.mjs rebase` |
| Publish | `node scripts/fork-release.mjs publish` |
