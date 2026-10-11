#!/bin/sh
# PuffPAI pre-commit secret scan.
# Canonical copy lives at scripts/precommit-secret-scan.sh. Install by copying
# to the repo's hooks directory as `pre-commit`:
#   cp scripts/precommit-secret-scan.sh "$(git rev-parse --git-path hooks)/pre-commit"
# Worktrees share the main checkout's hooks directory, so one install covers all.

files=$(git diff --cached --name-only --diff-filter=ACM)
[ -z "$files" ] && exit 0

fail=0

# Credential-bearing filenames never belong in a commit, regardless of content.
name_block='(^|/)(\.env(\..*)?|api_key\.txt|go2rtc\.ya?ml|home_assistant_config\.json|id_rsa|id_ed25519|.*\.pem)$'

# Content that looks like a live secret: Anthropic keys, JWTs, AWS keys,
# private-key blocks, or long opaque values assigned to secret-shaped names.
content_block='sk-ant-[A-Za-z0-9_-]{16,}|eyJ[A-Za-z0-9_-]{20,}\.eyJ|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(api_key|apikey|api-key|access_token|password|secret)["'\'' ]*[:=]["'\'' ]*[A-Za-z0-9+/_-]{24,}'

for f in $files; do
  case "$f" in
    *.example|*.sample) continue ;;
  esac
  if printf '%s\n' "$f" | grep -qE "$name_block"; then
    echo "pre-commit: BLOCKED $f (credential-bearing filename)"
    fail=1
    continue
  fi
  if git show ":$f" 2>/dev/null | grep -qiE "$content_block"; then
    echo "pre-commit: BLOCKED $f (content matches a secret pattern)"
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "pre-commit: commit refused by the secret scan (scripts/precommit-secret-scan.sh)."
  echo "If the match is a real secret: remove it AND rotate it — it is burned."
  echo "If it is a false positive, rename it *.example or restructure the line."
  exit 1
fi
exit 0
