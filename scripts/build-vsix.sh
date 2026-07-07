#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORE="$ROOT/ai-dev-core"
EXT="$ROOT/ai-dev-vscode"
ARTIFACTS="$ROOT/artifacts"

mkdir -p "$ARTIFACTS"

echo "Cleaning previous VSIX artifacts..."
rm -f "$ARTIFACTS"/*.vsix

echo "Vendoring ai-dev-core into ai-dev-vscode..."
rm -rf "$EXT/vendor/ai-dev-core"
mkdir -p "$EXT/vendor"

rsync -a \
  --exclude ".git" \
  --exclude "node_modules" \
  --exclude "out" \
  --exclude "dist" \
  --exclude "artifacts" \
  "$CORE/" \
  "$EXT/vendor/ai-dev-core/"

echo "Installing extension dependencies..."
cd "$EXT"
npm ci

echo "Compiling extension..."
npm run compile

VERSION="$(node -p "require('./package.json').version")"
VSIX="$ARTIFACTS/ai-dev-vscode-${VERSION}.vsix"

echo "Packaging VSIX..."
npx --yes @vscode/vsce package --out "$VSIX"

echo
echo "Built artifact:"
ls -la "$VSIX"
