#!/usr/bin/env bash
# Run the packaged app under strace and fail if it (or any helper process it
# starts) creates an internet socket (AF_INET / AF_INET6) or connects to one.
# Local AF_UNIX sockets (the X server, D-Bus) are allowed.
#   packaging/linux/no_network_check.sh dist/mef3io-studio/mef3io-studio --self-test-gui
set -euo pipefail
TRACE="$(mktemp)"
strace -f -qq -e trace=socket,connect,bind,sendto -o "$TRACE" "$@"
echo "strace: $(grep -c . "$TRACE") socket-related calls; families used:"
grep -oE 'AF_[A-Z0-9]+' "$TRACE" | sort | uniq -c || true
if grep -E 'AF_INET6?\b|sin6?_addr' "$TRACE"; then
  echo "FAILED: the app opened an internet socket"
  exit 1
fi
echo "OK: no internet sockets"
