#!/usr/bin/env bash
# Regression guard: every workflow that runs Claude goes through the
# AutomaticAI gateway (https://api.automaticai.io), never api.anthropic.com.
# The code-review workflow runs it as its first step, so a regression fails the
# job before any model call is made. Usage:
#   bash .github/scripts/check-gateway-routing.sh [workflows-dir]
#
# A workflow fails when it:
#   - uses the subscription OAuth token (claude_code_oauth_token /
#     CLAUDE_CODE_OAUTH_TOKEN), which talks to api.anthropic.com directly;
#   - names api.anthropic.com anywhere outside a comment;
#   - runs claude-code-action without ANTHROPIC_BASE_URL set to the gateway,
#     without anthropic_api_key from secrets.AUTOMATICAI_GATEWAY_TOKEN, or
#     without github_token (the action's OIDC app-token exchange calls
#     api.anthropic.com);
#   - leaves classify_inline_comments on (its post-step POSTs the key to a
#     hard-coded https://api.anthropic.com/v1/messages);
#   - runs claude-code-action without `--model automaticai/...` in claude_args,
#     with any other --model there, or with an OPUS/SONNET/HAIKU default model
#     slot that is not an automaticai/ gateway route.
# Comment lines are ignored so headers can explain what is forbidden.
# Pattern: delorenj/heyma-obsidian 68a7810 + 1cd9f06 (AAI-59).
set -uo pipefail

GATEWAY="https://api.automaticai.io"
dir="${1:-$(dirname "$0")/../workflows}"
status=0
count=0
fail() { echo "  $1: $2" >&2; status=1; }

# Values of `key:` lines, unquoted, one per line.
values() {
  grep -oE "$1:[[:space:]]*['\"]?[^[:space:]'\"]+" <<<"$2" |
    sed -E "s/^$1:[[:space:]]*['\"]?//" || true
}

# claude_args values, inline or as a YAML block scalar (| or >).
claude_args() {
  awk '
    block {
      if ($0 ~ /^[[:space:]]*$/) next
      match($0, /^[[:space:]]*/)
      if (RLENGTH > indent) { print; next }
      block = 0
    }
    /^[[:space:]]*claude_args:/ {
      match($0, /^[[:space:]]*/); indent = RLENGTH
      value = $0; sub(/^[[:space:]]*claude_args:[[:space:]]*/, "", value)
      if (value ~ /^[|>][+-]?[[:space:]]*$/) block = 1; else print value
    }
  ' <<<"$1"
}

shopt -s nullglob
for file in "$dir"/*.yml "$dir"/*.yaml; do
  count=$((count + 1))
  name=$(basename "$file")
  code=$(grep -vE '^[[:space:]]*#' "$file" || true)

  grep -qiE 'claude_code_oauth_token' <<<"$code" &&
    fail "$name" "uses claude_code_oauth_token / CLAUDE_CODE_OAUTH_TOKEN (direct Anthropic subscription auth)"
  grep -qE 'api\.anthropic\.com' <<<"$code" &&
    fail "$name" "names api.anthropic.com; inference must go through $GATEWAY"
  grep -qE 'anthropics/claude-code(-action)?@' <<<"$code" || continue

  bases=$(values ANTHROPIC_BASE_URL "$code")
  [ -n "$bases" ] || fail "$name" "claude-code-action needs ANTHROPIC_BASE_URL: $GATEWAY"
  while read -r base; do
    [ -z "$base" ] || [ "${base%/}" = "$GATEWAY" ] || fail "$name" "ANTHROPIC_BASE_URL $base is not $GATEWAY"
  done <<<"$bases"
  grep -qE 'anthropic_api_key:[[:space:]]*\$\{\{[[:space:]]*secrets\.AUTOMATICAI_GATEWAY_TOKEN[[:space:]]*\}\}' <<<"$code" ||
    fail "$name" "claude-code-action must authenticate with secrets.AUTOMATICAI_GATEWAY_TOKEN"
  grep -qE 'github_token:' <<<"$code" ||
    fail "$name" "pass github_token (the action's OIDC app-token exchange calls api.anthropic.com)"
  grep -qE "classify_inline_comments:[[:space:]]*['\"]?false['\"]?" <<<"$code" ||
    fail "$name" "set classify_inline_comments: 'false' (its post-step calls api.anthropic.com directly)"

  models=$(claude_args "$code" | grep -oE -- "--model(=|[[:space:]]+)['\"]?[^[:space:]'\"]+" |
    sed -E "s/^--model(=|[[:space:]]+)['\"]?//" || true)
  [ -n "$models" ] || fail "$name" "claude_args must pass --model automaticai/<account>/<model>"
  while read -r model; do
    [ -z "$model" ] || [[ "$model" == automaticai/* ]] ||
      fail "$name" "claude_args --model $model must be an automaticai/ gateway route"
  done <<<"$models"

  for slot in OPUS SONNET HAIKU; do
    slots=$(values "ANTHROPIC_DEFAULT_${slot}_MODEL" "$code")
    [ -n "$slots" ] || fail "$name" "ANTHROPIC_DEFAULT_${slot}_MODEL must be an automaticai/ gateway route"
    while read -r model; do
      [ -z "$model" ] || [[ "$model" == automaticai/* ]] ||
        fail "$name" "ANTHROPIC_DEFAULT_${slot}_MODEL $model must be an automaticai/ gateway route"
    done <<<"$slots"
  done
done

if [ "$status" -ne 0 ]; then
  echo "Gateway routing check failed." >&2
  exit 1
fi
echo "Gateway routing check passed ($count workflows)."
