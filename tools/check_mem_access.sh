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
#   The script prints the ancestry so the responsible app is visible, and it
#   also reports which apps are currently granted, by asking tccd the only way
#   available without Full Disk Access: try each candidate as the caller.
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
#include <unistd.h>
#include <libproc.h>

static pid_t ppid_of(pid_t pid) {
    int n = proc_listpids(PROC_ALL_PIDS, 0, NULL, 0);
    if (n <= 0) return 0;
    pid_t *pids = calloc((size_t)n + 1, sizeof(pid_t));
    proc_listpids(PROC_ALL_PIDS, 0, pids, (int)((n + 1) * sizeof(pid_t)));
    pid_t out = 0;
    for (int j = 0; j < n; j++) {
        if (pids[j] == pid) continue;
        struct proc_bsdshortinfo bi;
        if (proc_pidinfo(pids[j], PROC_PIDTBSDINFO, 0, &bi, sizeof(bi)) > 0 &&
            bi.pbsi_ppid == pid) { out = pids[j]; break; }
    }
    free(pids);
    return out;
}

int main(int argc, char **argv) {
    pid_t target = (pid_t)atoi(argv[1]);
    pid_t cur = getpid();
    char chain[8][512];
    pid_t cpid[8];
    int depth = 0;
    for (int i = 0; i < 7 && cur > 1 && depth < 8; i++) {
        proc_name(cur, chain[depth], 512);
        cpid[depth] = cur;
        depth++;
        pid_t next = ppid_of(cur);
        if (next <= 1) break;
        cur = next;
    }
    printf("ancestry    :\n");
    for (int i = 0; i < depth; i++)
        printf("   %-8d %s%s\n", cpid[i], chain[i],
               i == depth - 1 ? "   <- topmost visible (likely the responsible app)" : "");
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
echo "TCC service: kTCCServiceDeveloperTool exists on this macOS"
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
  echo "The 'ancestry' block above names the app that spawned this shell."
  echo "That app -- and only that app -- needs the TCC 'Developer Tools' grant:"
  echo "    System Settings -> Privacy & Security -> Developer Tools"
  echo "    add the app with '+', turn its switch on, then run this script again"
  echo "    FROM THAT SAME APP."
  echo
  echo "Common mistake: granting Terminal but running from VS Code / DSH, or"
  echo "vice versa. The grant is matched against the responsible app."
fi
