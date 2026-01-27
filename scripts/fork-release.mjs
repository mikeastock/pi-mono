#!/usr/bin/env node
/**
 * Fork release script for @mikeastock packages.
 *
 * Usage:
 *   node scripts/fork-release.mjs status          Show current fork version and upstream info
 *   node scripts/fork-release.mjs bump             Bump fork number (0.50.1-fork.1 -> 0.50.1-fork.2)
 *   node scripts/fork-release.mjs rebase [version]  Set base to upstream version (auto-detects latest tag)
 *   node scripts/fork-release.mjs publish           Publish to npm with --tag fork
 */

import { execSync } from "child_process";
import { readFileSync, writeFileSync } from "fs";

const PKG_PATH = "packages/mom/package.json";
const UPSTREAM_REMOTE = "upstream";
const PRERELEASE_ID = "fork";

function run(cmd, options = {}) {
	try {
		const result = execSync(cmd, { encoding: "utf-8", stdio: "pipe", ...options });
		return result?.trim() ?? null;
	} catch (e) {
		if (!options.ignoreError) {
			console.error(`Command failed: ${cmd}`);
			console.error(e.stderr?.trim() || e.message);
			process.exit(1);
		}
		return null;
	}
}

function readPkg() {
	return JSON.parse(readFileSync(PKG_PATH, "utf-8"));
}

function writePkg(pkg) {
	writeFileSync(PKG_PATH, JSON.stringify(pkg, null, "\t") + "\n");
}

/** Parse a fork version string like "0.50.1-fork.3" */
function parseForkVersion(version) {
	const match = version.match(/^(\d+\.\d+\.\d+)(?:-fork\.(\d+))?$/);
	if (!match) {
		console.error(`Cannot parse version: ${version}`);
		console.error(`Expected format: X.Y.Z or X.Y.Z-fork.N`);
		process.exit(1);
	}
	return {
		base: match[1],
		forkNum: match[2] ? parseInt(match[2], 10) : null,
	};
}

function buildForkVersion(base, forkNum) {
	return `${base}-${PRERELEASE_ID}.${forkNum}`;
}

/** Fetch upstream tags and return the latest semver version tag. */
function getLatestUpstreamVersion() {
	run(`git fetch ${UPSTREAM_REMOTE} --tags`);
	const tags = run("git tag --list 'v*' --sort=-version:refname");
	if (!tags) {
		console.error("No version tags found.");
		process.exit(1);
	}
	const latest = tags.split("\n")[0];
	return latest.replace(/^v/, "");
}

// --- Commands ---

function status() {
	const pkg = readPkg();
	const { base, forkNum } = parseForkVersion(pkg.version);

	console.log(`Package:      ${pkg.name}`);
	console.log(`Version:      ${pkg.version}`);
	console.log(`Upstream base: ${base}`);
	console.log(`Fork number:  ${forkNum ?? "(not a fork version)"}`);

	console.log("\nFetching upstream tags...");
	const latest = getLatestUpstreamVersion();
	console.log(`Latest upstream: ${latest}`);

	if (latest !== base) {
		console.log(`\n  Upstream has moved to ${latest}. Run: node scripts/fork-release.mjs rebase`);
	} else {
		console.log("\n  Up to date with upstream.");
	}
}

function bump() {
	const pkg = readPkg();
	const { base, forkNum } = parseForkVersion(pkg.version);

	if (forkNum === null) {
		console.error(`Current version (${pkg.version}) is not a fork version.`);
		console.error(`Run "rebase" first to set the fork version.`);
		process.exit(1);
	}

	const newVersion = buildForkVersion(base, forkNum + 1);
	pkg.version = newVersion;
	writePkg(pkg);

	console.log(`Bumped: ${base}-${PRERELEASE_ID}.${forkNum} -> ${newVersion}`);
	console.log(`Updated ${PKG_PATH}`);
}

function rebase(explicitVersion) {
	const pkg = readPkg();
	const oldVersion = pkg.version;

	let base;
	if (explicitVersion) {
		base = explicitVersion.replace(/^v/, "");
	} else {
		console.log("Fetching upstream tags...");
		base = getLatestUpstreamVersion();
	}

	const newVersion = buildForkVersion(base, 1);
	pkg.version = newVersion;
	writePkg(pkg);

	console.log(`Rebased: ${oldVersion} -> ${newVersion}`);
	console.log(`Updated ${PKG_PATH}`);
}

function publish() {
	const pkg = readPkg();
	const { forkNum } = parseForkVersion(pkg.version);

	if (forkNum === null) {
		console.error(`Current version (${pkg.version}) is not a fork version.`);
		console.error(`Run "rebase" first to set the fork version.`);
		process.exit(1);
	}

	console.log(`Publishing ${pkg.name}@${pkg.version}...`);
	run("npm publish --access public --tag fork", {
		cwd: "packages/mom",
		stdio: "inherit",
	});
	console.log(`\nPublished ${pkg.name}@${pkg.version} with tag "fork"`);
}

// --- Main ---

const command = process.argv[2];
const arg = process.argv[3];

switch (command) {
	case "status":
		status();
		break;
	case "bump":
		bump();
		break;
	case "rebase":
		rebase(arg);
		break;
	case "publish":
		publish();
		break;
	default:
		console.error("Usage: node scripts/fork-release.mjs <status|bump|rebase|publish> [args]");
		console.error("");
		console.error("Commands:");
		console.error("  status              Show current fork version and upstream info");
		console.error("  bump                Bump fork number (0.50.1-fork.1 -> 0.50.1-fork.2)");
		console.error("  rebase [version]    Set base to upstream version (auto-detects latest tag)");
		console.error("  publish             Publish to npm with --tag fork");
		process.exit(1);
}
