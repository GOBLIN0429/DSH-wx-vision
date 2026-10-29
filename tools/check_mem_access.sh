#!/usr/bin/env bash
# Probe whether this shell can obtain a task port for the WeChat process.
#
# WHY EARLIER ATTEMPTS FAILED
#   WeChat is signed with the hardened runtime (flags=0x10000(runtime)).
#   taskgated grants a task port only to a caller whose "responsible process"
#   holds the TCC "Developer Tools" grant. Root alone is NOT enough, and the
#   grant must belong to the app that actually spawns the shell:
#
#     Terminal.app   -> grant Terminal.app,  run from Terminal.app
#     VS Code        -> grant VS Code,       run from VS Code's terminal
#     DSH Desktop    -> grant DSH Desktop,   run from DSH's shell
#
#   The script prints the ancestry so the responsible app is visible. The top
#   of the chain is the app that needs the grant.
#
# This only READS. It does not modify WeChat, inject anything, or change the
# app's code signature.
set -uo pipefail

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/probe.c" <<'EOF'
#include <mach/mach.h>
#include <sys/types.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <libproc.h>
#include <sys/proc_info.h>

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: probe <pid>\n"); return 2; }
    pid_t target = (pid_t)atoi(argv[1]);

    pid_t cur = getpid();
    char top[1024] = {0};
    printf("ancestry    :\n");
    for (int i = 0; i < 10 && cur > 1; i++) {
        char path[1024] = {0};
        int len = proc_pidpath(cur, path, sizeof(path));
        struct proc_bsdinfo bi;
        memset(&bi, 0, sizeof(bi));
        pid_t pp = 0;
        if (proc_pidinfo(cur, PROC_PIDTBSDINFO, 0, &bi, sizeof(bi)) > 0)
            pp = bi.pbi_ppid;
        if (i == 0) snprintf(top, sizeof(top), "%s", len > 0 ? path : "?");
        printf("   %-8d %s\n", cur, len > 0 ? path : "?");
        if (pp <= 1) break;
        cur = pp;
    }

    task_t t = MACH_PORT_NULL;
    kern_return_t kr = task_for_pid(mach_task_self(), target, &t);
    printf("task_for_pid : %d", kr);
    if (kr == KERN_SUCCESS) printf("  -> OK, WeChat memory is readable\n");
    else if (kr == 5) printf("  -> KERN_FAILURE (grant missing for the responsible app)\n");
    else printf("  -> %s\n", mach_error_string(kr));
    return kr == KERN_SUCCESS ? 0 : 1;
}
EOF

clang -o "$TMP/probe" "$TMP/probe.c" || { echo "clang failed"; exit 2; }

PID="$(pgrep -x WeChat | head -1)"
if [[ -z "$PID" ]]; then
  echo "WeChat is not running. Start it first."
  exit 2
fi

echo "macOS      : $(sw_vers -productVersion) ($(uname -m))"
echo "WeChat pid : $PID"
echo "signature  : $(codesign -dv /Applications/微信.app 2>&1 | grep -E 'flags=' | head -1)"
echo

echo "== without sudo =="
"$TMP/probe" "$PID" || true
echo
echo "== with sudo =="
sudo "$TMP/probe" "$PID" || true
echo

if sudo "$TMP/probe" "$PID" 2>/dev/null | grep -q "OK"; then
  echo "==> Memory route VIABLE."
  echo "    Next: scan WeChat memory for the SQLCipher key."
else
  echo "==> Still blocked."
  echo
  echo "Grant TCC 'Developer Tools' to the app at the TOP of the ancestry chain"
  echo "printed above (that is the responsible app), then run this script again"
  echo "FROM THAT SAME APP:"
  echo "    System Settings -> Privacy & Security -> Developer Tools"
  echo "    add the app with '+', turn its switch on"
fi
