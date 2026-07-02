#!/usr/bin/env bash
# Install the repo git hooks. Run once after cloning: ./tools/install_hooks.sh
set -e
cd "$(dirname "$0")/.."

mkdir -p .git/hooks
cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
# Block any push while the privacy scan has findings.
exec python3 tools/privacy_scan.py
HOOK
chmod +x .git/hooks/pre-push
echo "pre-push privacy scan installed"
