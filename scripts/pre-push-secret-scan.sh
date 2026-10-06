#!/bin/sh
# Blocks a push when any commit being pushed adds a secret-like path or secret-like content.
# Install: cp scripts/pre-push-secret-scan.sh .git/hooks/pre-push && chmod +x .git/hooks/pre-push
set -e
zero=0000000000000000000000000000000000000000
status=0
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "$zero" ] && continue
  if [ "$remote_sha" = "$zero" ]; then range="$local_sha"; else range="$remote_sha..$local_sha"; fi
  bad_paths=$(git log --format= --name-only --diff-filter=A "$range" | grep -E '(^|/)\.env($|\.)|\.pk$|\.pem$|(^|/)data/' | grep -v '\.env\.example$' || true)
  if [ -n "$bad_paths" ]; then echo "pre-push: secret-like path(s) in pushed commits:"; echo "$bad_paths"; status=1; fi
  bad_content=$(git log -p --format= "$range" -- . ':!tests/fixtures' ':!uv.lock' | grep -E '^\+' | grep -E '0x[0-9a-fA-F]{64}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|^\+(POLY_PK|POLY_FUNDER|KALSHI_API_KEY)=.+' || true)
  if [ -n "$bad_content" ]; then echo "pre-push: secret-like content in pushed commits:"; echo "$bad_content" | cut -c1-80; status=1; fi
done
[ $status -ne 0 ] && echo "pre-push: push blocked" 
exit $status
