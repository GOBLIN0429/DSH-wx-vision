#!/usr/bin/env bash
# Check whether we can attach to the WeChat process and read its memory.
#
# WeChat 4.x stores messages in SQLCipher databases:
#   ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/*/db_storage/message/
# The key lives in the WeChat process's memory and is never written to disk.
# If we can obtain a task port for that process we can read the key out and
# query the databases directly -- no screenshots, no OCR, no code injection, and
# the WeChat app itself is never modified.
#
# WHY A PLAIN `sudo` RUN STILL FAILS
#   WeChat is signed with the hardened runtime (flags=0x10000(runtime)), so
#   taskgated only hands over a task port to a caller whose "responsible
#   process" already holds the TCC "Developer Tools" grant. sudo alone is NOT
#   enough: root from a terminal without that grant still gets KERN_FAILURE.
#
#   Fix: System Settings -> Privacy & Security -> Developer Tools
#         -> add Terminal.app and enable its switch
#   Then run this script again FROM Terminal.app (the responsible app must be
#   the one holding the grant).
#
#   macOS 26 note: the panel lists apps that use debuggers or task ports.
#   Terminal is not on the list by default, so add it with the "+" button.
#
# This only READS. It does not modify WeChat, does not inject anything, and does
# not change the app's code signature.
set -uo pipefail

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/tfp.c" <<'EOF'
#include <mach/mach.h>
#include <sys/types.h>
#include <stdlib.h>
#include <stdio.h>

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: tfp <pid>\n"); return 2; }
    pid_t pid = (pid_t)atoi(argv[1]);
    task_t t = MACH_PORT_NULL;
    kern_return_t kr = task_for_pid(mach_task_self(), pid, &t);
    printf("task_for_pid(%d) = %d\n", pid, kr);
    if (kr == KERN_SUCCESS) {
        printf("  -> OK: task port acquired, WeChat memory is readable\n");
        return 0;
    }
    if (kr == 5) printf("  -> KERN_FAILURE: need TCC 'Developer Tools' for THIS terminal app\n");
    else printf("  -> error %d (%s)\n", kr, mach_error_string(kr));
    return 1;
}
EOF

clang -o "$TMP/tfp" "$TMP/tfp.c" || { echo "clang failed"; exit 2; }

PID="$(pgrep -x WeChat | head -1)"
if [[ -z "$PID" ]]; then
  echo "WeChat is not running. Start it first."
  exit 2
fi

echo "macOS      : $(sw_vers -productVersion) ($(uname -m))"
echo "WeChat pid : $PID"
echo "signature  : $(codesign -dv /Applications/微信.app 2>&1 | grep -E 'flags=' | head -1)"
echo "SIP        : $(csrutil status 2>/dev/null | head -1)"
echo

echo "== without sudo =="
"$TMP/tfp" "$PID" || true
echo

echo "== with sudo =="
sudo "$TMP/tfp" "$PID" || true
echo

if sudo "$TMP/tfp" "$PID" 2>/dev/null | grep -q "OK"; then
  echo "==> Memory route VIABLE on this machine."
  echo "    Next: scan WeChat memory for the SQLCipher key."
else
  echo "==> Memory route BLOCKED. Grant TCC 'Developer Tools' to Terminal.app:"
  echo "    System Settings -> Privacy & Security -> Developer Tools -> add/enable Terminal"
  echo "    then run this script again from Terminal.app."
fi
