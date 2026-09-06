# Map OpenShell credential placeholders onto the variable names tools expect.
#
# An attached provider's credential is injected under the credential's NAME
# from its profile (`api_token` for the github profile). A profile's `env_vars`
# list is NOT used for injection — openshell-providers' discovery.rs reads it
# when creating a provider from the creator's environment — so nothing sets
# GIT_TOKEN / GITHUB_TOKEN on its own and every git call fails to authenticate.
#
# The value is an opaque `openshell:resolve:...` placeholder that the sandbox
# proxy swaps for the real secret in flight, so copying it between variables
# exposes nothing: the secret itself never enters the sandbox.
case $- in *i*) return 0 ;; esac
[ -n "${api_token:-}" ] || return 0

# GIT_TOKEN/GIT_USERNAME drive the host image's git credential helper, and both
# are on Omnigent's runner env allowlist, so agents inherit them.
export GIT_TOKEN="${GIT_TOKEN:-$api_token}"
export GIT_USERNAME="${GIT_USERNAME:-x-access-token}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-$api_token}"
export GH_TOKEN="${GH_TOKEN:-$api_token}"

# opencode and pi authenticate from auth.json files rather than env vars, so an
# injected placeholder only reaches them if it is written into that file. Swap
# just the alibaba-token-plan entry (the qwen-token-plan provider's endpoint is
# the one bound in policy.yaml); anything else in the file is left alone.
if [ -n "${OPENAI_API_KEY:-}" ] && [ -f "$HOME/.local/share/opencode/auth.json" ]; then
  python3 - "$HOME/.local/share/opencode/auth.json" "$OPENAI_API_KEY" <<'PYEOF' 2>/dev/null || true
import json, sys
path, placeholder = sys.argv[1], sys.argv[2]
with open(path) as fh:
    auth = json.load(fh)
entry = auth.get("alibaba-token-plan")
if isinstance(entry, dict) and entry.get("key") != placeholder:
    entry["key"] = placeholder
    with open(path, "w") as fh:
        json.dump(auth, fh)
PYEOF
fi

