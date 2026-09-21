#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Stopping backend :8230..."
lsof -ti tcp:8230 2>/dev/null | xargs kill 2>/dev/null || true
sleep 0.5

echo "==> Building frontend..."
cd "$ROOT/frontend"
npm run build

echo "==> Building Tauri app..."
npx tauri build

APP="$ROOT/frontend/src-tauri/target/release/bundle/macos/一问.app"
echo "==> Launching $APP"
open "$APP"
