#!/usr/bin/env node

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const outputPath = path.join(root, "out.txt");
const corePath = path.join(root, "ai-dev-core");
const extensionPath = path.join(root, "ai-dev-vscode");
const vendorPath = path.join(extensionPath, "vendor", "ai-dev-core");
const artifactsPath = path.join(root, "artifacts");

function appendLog(text = "") {
    const line = String(text);
    process.stdout.write(`${line}\n`);
    fs.appendFileSync(outputPath, `${line}\n`, "utf8");
}

function section(title) {
    appendLog();
    appendLog(`===== ${title} =====`);
}

function fail(message) {
    throw new Error(message);
}

function requirePath(targetPath, description) {
    if (!fs.existsSync(targetPath)) {
        fail(`${description} not found: ${targetPath}`);
    }
}

function commandExists(command) {
    const lookup = process.platform === "win32"
        ? spawnSync("where.exe", [command], { encoding: "utf8", windowsHide: true })
        : spawnSync("sh", ["-c", 'command -v "$1" >/dev/null 2>&1', "sh", command], { encoding: "utf8" });

    return lookup.status === 0;
}

function requireCommand(command) {
    if (!commandExists(command)) {
        fail(`${command} was not found on PATH.`);
    }
}

function runCommand(title, command, commandArgs, cwd = root) {
    section(title);
    appendLog(`Command: ${command} ${commandArgs.join(" ")}`);

    const result = spawnSync(command, commandArgs, {
        cwd,
        encoding: "utf8",
        shell: false,
        windowsHide: true,
        maxBuffer: 50 * 1024 * 1024,
    });

    if (result.stdout) {
        for (const line of result.stdout.replace(/\s+$/, "").split(/\r?\n/)) {
            appendLog(line);
        }
    }

    if (result.stderr) {
        for (const line of result.stderr.replace(/\s+$/, "").split(/\r?\n/)) {
            appendLog(line);
        }
    }

    if (result.error) {
        throw result.error;
    }

    const exitCode = typeof result.status === "number" ? result.status : 1;
    appendLog(`ExitCode: ${exitCode}`);

    if (exitCode !== 0) {
        fail(`${title} failed with exit code ${exitCode}.`);
    }
}

function removePreviousArtifacts() {
    fs.mkdirSync(artifactsPath, { recursive: true });

    for (const entry of fs.readdirSync(artifactsPath, { withFileTypes: true })) {
        if (entry.isFile() && entry.name.toLowerCase().endsWith(".vsix")) {
            fs.rmSync(path.join(artifactsPath, entry.name), { force: true });
        }
    }
}

function shouldCopy(sourcePath) {
    const relativePath = path.relative(corePath, sourcePath);

    if (!relativePath) {
        return true;
    }

    const parts = relativePath.split(path.sep);
    const excludedDirectories = new Set([
        ".git",
        "node_modules",
        "out",
        "dist",
        "artifacts",
    ]);

    if (parts.some((part) => excludedDirectories.has(part))) {
        return false;
    }

    return path.basename(relativePath) !== "out.txt";
}

function vendorCore() {
    fs.rmSync(vendorPath, { recursive: true, force: true });
    fs.mkdirSync(path.dirname(vendorPath), { recursive: true });

    fs.cpSync(corePath, vendorPath, {
        recursive: true,
        force: true,
        filter: shouldCopy,
    });
}

function readExtensionVersion() {
    const packageJsonPath = path.join(extensionPath, "package.json");
    const packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));

    if (typeof packageJson.version !== "string" || !packageJson.version.trim()) {
        fail(`Extension version is missing or invalid in ${packageJsonPath}.`);
    }

    return packageJson.version.trim();
}

function listInstalledExtension(codeCommand) {
    const result = spawnSync(codeCommand, ["--list-extensions", "--show-versions"], {
        cwd: root,
        encoding: "utf8",
        shell: false,
        windowsHide: true,
        maxBuffer: 10 * 1024 * 1024,
    });

    if (result.error) {
        throw result.error;
    }

    if (result.status !== 0) {
        fail(`Listing installed VS Code extensions failed with exit code ${result.status}.`);
    }

    return result.stdout
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => /ai-dev/i.test(line));
}

function main() {
    const args = process.argv.slice(2);
    const supportedArgs = new Set(["--install"]);

    for (const arg of args) {
        if (!supportedArgs.has(arg)) {
            throw new Error(`Unknown argument: ${arg}`);
        }
    }

    const installAfterBuild = args.includes("--install");

    fs.rmSync(outputPath, { force: true });
    fs.writeFileSync(outputPath, "===== build local AI Dev plugin =====\n", "utf8");

    appendLog(`Generated: ${new Date().toISOString()}`);
    appendLog(`Repository: ${root}`);
    appendLog("Source policy: use current local files; no Git pull");
    appendLog(`Install after build: ${installAfterBuild ? "yes" : "no"}`);

    requirePath(corePath, "ai-dev-core");
    requirePath(extensionPath, "ai-dev-vscode");
    requirePath(path.join(extensionPath, "package.json"), "Extension package.json");

    const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
    const npxCommand = process.platform === "win32" ? "npx.cmd" : "npx";
    const codeCommand = process.platform === "win32" ? "code.cmd" : "code";

    requireCommand("git");
    requireCommand(npmCommand);
    requireCommand(npxCommand);

    if (installAfterBuild) {
        requireCommand(codeCommand);
    }

    runCommand("local source status", "git", ["-C", root, "status", "--short", "--branch"]);

    section("clean previous VSIX artifacts");
    removePreviousArtifacts();
    appendLog("Previous VSIX artifacts removed.");

    section("vendor local ai-dev-core");
    vendorCore();
    appendLog(`Source: ${corePath}`);
    appendLog(`Destination: ${vendorPath}`);

    runCommand("install extension dependencies", npmCommand, ["ci"], extensionPath);
    runCommand("compile extension", npmCommand, ["run", "compile"], extensionPath);

    const version = readExtensionVersion();
    const vsixPath = path.join(artifactsPath, `ai-dev-vscode-${version}.vsix`);

    runCommand(
        "package VSIX",
        npxCommand,
        ["--yes", "@vscode/vsce", "package", "--out", vsixPath],
        extensionPath,
    );

    requirePath(vsixPath, "Packaged VSIX");

    const artifact = fs.statSync(vsixPath);
    section("built artifact");
    appendLog(`Path: ${vsixPath}`);
    appendLog(`Size: ${artifact.size} bytes`);
    appendLog(`Modified: ${artifact.mtime.toISOString()}`);

    if (installAfterBuild) {
        runCommand(
            "install VSIX into VS Code",
            codeCommand,
            ["--install-extension", vsixPath, "--force"],
        );

        section("installed extension");
        const installed = listInstalledExtension(codeCommand);

        if (installed.length === 0) {
            appendLog("WARNING: VS Code did not report an extension matching 'ai-dev'.");
        } else {
            for (const line of installed) {
                appendLog(line);
            }
        }
    }

    section("build complete");
    appendLog(`Built: ${path.basename(vsixPath)}`);

    if (installAfterBuild) {
        appendLog(`Installed: ${path.basename(vsixPath)}`);
    }
}

try {
    main();
} catch (error) {
    try {
        section("build or installation failed");
        appendLog(error instanceof Error ? error.stack || error.message : String(error));
    } catch {
        // Preserve the original failure if logging also fails.
    }

    process.stderr.write("\nAI Dev plugin build or installation failed.\n");
    process.stderr.write(`Log: ${outputPath}\n`);
    process.exitCode = 1;
}
