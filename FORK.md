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

## Publishing

```bash
cd packages/mom
npm publish --access public --tag fork
```

The `--tag fork` dist-tag lets users install with:

```bash
npm install @mikeastock/pi-mom@fork
```

## Version Bumps

| Event | Action |
|---|---|
| Fork-specific changes | Bump the fork number: `0.50.1-fork.1` → `0.50.1-fork.2` |
| Rebase onto new upstream (e.g. `0.51.0`) | Reset to `0.51.0-fork.1` |
