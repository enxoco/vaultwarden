#!/bin/bash

# --- Helper Function for Cross-Platform Sed ---
replace_text() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s|$1|$2|g" "$3"
  else
    sed -i "s|$1|$2|g" "$3"
  fi
}

echo "🚀 UDS Army Workflow Initializer"
echo "--------------------------------"

# 1. Gather User Input for Template Replacements
read -rp "Enter GitHub Org Name [my-org-name]: " ORG_NAME
ORG_NAME=${ORG_NAME:-"my-org-name"}

read -rp "Enter Registry Username Variable Name [UDS_ARMY_REG_USERNAME]: " REG_USER_VAR
REG_USER_VAR=${REG_USER_VAR:-"UDS_ARMY_REG_USERNAME"}

read -rp "Enter Registry Password Variable Name [UDS_ARMY_REG_PASSWORD]: " REG_PASS_VAR
REG_PASS_VAR=${REG_PASS_VAR:-"UDS_ARMY_REG_PASSWORD"}

# 2. Download Template Files from GitHub
FILES=(
  ".github/workflows/ci.yaml|https://raw.githubusercontent.com/lemonprogis/uds-army-demo/refs/heads/main/.github/workflows/ci.yaml"
  ".github/actions/olm-cli-setup/action.yaml|https://raw.githubusercontent.com/lemonprogis/uds-army-demo/refs/heads/main/.github/actions/olm-cli-setup/action.yaml"
  ".github/actions/uds-cli-setup/action.yaml|https://raw.githubusercontent.com/lemonprogis/uds-army-demo/refs/heads/main/.github/actions/uds-cli-setup/action.yaml"
)

for item in "${FILES[@]}"; do
  LOCAL_PATH="${item%%|*}"
  REMOTE_URL="${item##*|}"
  echo "Downloading $LOCAL_PATH..."
  curl -s --create-dirs -o "$LOCAL_PATH" "$REMOTE_URL"
done

# 3. Perform Variable Replacements
TARGET_FILE=".github/workflows/ci.yaml"
if [ -f "$TARGET_FILE" ]; then
  replace_text "uds-army-demo" "$ORG_NAME" "$TARGET_FILE"
  replace_text "DEMO_ORG_USER_ID" "\${{ secrets.$REG_USER_VAR }}" "$TARGET_FILE"
  replace_text "DEMO_ORG_PASSWORD" "\${{ secrets.$REG_PASS_VAR }}" "$TARGET_FILE"
  echo "✅ Updated $TARGET_FILE with Org and Secret references."
fi

# 4. Detect linter and apply caching to CI workflow
DETECT_LINTER=".github/scripts/detect-linter.py"
if command -v python3 &>/dev/null && [ -f "$DETECT_LINTER" ]; then
  echo -e "\n🔍 Detecting linter..."
  python3 "$DETECT_LINTER"
else
  echo "⚠️  python3 or $DETECT_LINTER not found. Skipping linter detection."
  echo "   Run 'python3 $DETECT_LINTER' manually to configure linting steps."
fi

# 5. Interactive Secret Creation via GH CLI
echo -e "\n🔐 Checking for required GitHub Secrets..."

if ! command -v gh &> /dev/null; then
  echo "⚠️  GH CLI not found. Skipping secret creation."
else
  # Check if we are in a git repo and logged in
  if gh auth status &>/dev/null && git rev-parse --is-inside-work-tree &>/dev/null; then
    
    # regex to find 'secrets.VARIABLE_NAME' and extract only 'VARIABLE_NAME'
    # works by looking for "secrets." and grabbing the alphanumeric/underscore string after it
    SECRETS_FOUND=$(grep -oE "secrets\.[a-zA-Z0-9_]+" "$TARGET_FILE" | cut -d. -f2 | sort -u)

    for SECRET_NAME in $SECRETS_FOUND; do
      read -rp "Enter value for secret '$SECRET_NAME' (leave blank to skip): " SECRET_VALUE
      if [ -n "$SECRET_VALUE" ]; then
        echo -n "$SECRET_VALUE" | gh secret set "$SECRET_NAME"
        echo "✅ Secret '$SECRET_NAME' set in GitHub."
      else
        echo "⏭️  Skipped '$SECRET_NAME'."
      fi
    done
  else
    echo "⚠️  Not logged into 'gh' or not in a git repo. Skipping secret upload."
  fi
fi

echo -e "\n✨ All set! Your CI/CD environment is ready."

## Can you push a package to registry.uds.run?

# zarf tools registry login \
#     -u SA_F363-8A5C-38CA \
#     -p dc8c1065b04afda04bec2fe074628247 \
#     registry.uds.run

# # Maybe grab a simple demo package here?
# zarf package publish zarf-package-vaultwarden-arm64-dev-upstream.tar.zst oci://registry.uds.run/mike-demo-org/vaultwarden