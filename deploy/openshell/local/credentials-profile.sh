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
