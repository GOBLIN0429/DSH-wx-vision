#!/usr/bin/env python3
"""Session-level control for the WeChat bridge.

Stopping the bridge process is NOT enough: any prompt already injected into a
DSH session keeps that session's agent working — it will keep activating
WeChat and typing. So a clean stop must also cancel the session and prove its
queue is empty.

Commands:
    status                 print target session, running state and queue depth
    cancel <sessionId>     cancel a session and wait for its queue to drain
    any-running            list every session that is currently running
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wxbridge  # noqa: E402


def sessions(h):
    return h.sessions()


def find(items, sid):
    for it in items:
        if it["sessionId"] == sid:
            return it
    return None


def queue_of(item):
    p = (item.get("projections") or {}).get("values") or {}
    box = p.get("inbox") or {}
    return (box.get("next-turn") or [], box.get("next-step") or [])


def cmd_status(argv):
    h = wxbridge.Harness()
    if not h.login():
        print("ERROR: cannot authenticate to harness")
        return 2
    sid = argv[0] if argv else None
    items = sessions(h)
    if sid:
        it = find(items, sid)
        if it is None:
            print(f"session {sid}: not found")
            return 1
        nt, ns = queue_of(it)
        print(f"session   : {sid}")
        print(f"running   : {it.get('running')}")
        print(f"agent     : {it.get('agentAvailable')}")
        print(f"queue     : next-turn={len(nt)} next-step={len(ns)}")
        return 0
    running = [i for i in items if i.get("running")]
    print(f"sessions  : {len(items)} total, {len(running)} running")
    for i in running:
        p = (i.get("projections") or {}).get("values") or {}
        nt, ns = queue_of(i)
        print(f"  RUNNING {i['sessionId']}  title={p.get('title')!r}  "
              f"queue={len(nt)}/{len(ns)}")
    return 0


def cmd_cancel(argv):
    if not argv:
        print("usage: cancel <sessionId>")
        return 2
    sid = argv[0]
    h = wxbridge.Harness()
    if not h.login():
        print("ERROR: cannot authenticate to harness")
        return 2
    r = h.call("session/cancel", {"request": {"sessionId": sid}})
    ok = (r.get("result") or {}).get("ok")
    print(f"cancel accepted: {ok}")
    # Wait for the agent to actually stop; cancel is cooperative.
    import time
    for _ in range(20):
        it = find(sessions(h), sid)
        if it is None:
            print("session no longer present")
            return 0
        nt, ns = queue_of(it)
        if not it.get("running") and not nt and not ns:
            print("session stopped, queue empty")
            return 0
        time.sleep(0.5)
    it = find(sessions(h), sid)
    if it:
        nt, ns = queue_of(it)
        print(f"WARNING: still running={it.get('running')} queue={len(nt)}/{len(ns)}")
    return 1


def cmd_any_running(argv):
    h = wxbridge.Harness()
    if not h.login():
        return 2
    running = [i for i in sessions(h) if i.get("running")]
    for i in running:
        print(i["sessionId"])
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "status":
        return cmd_status(rest)
    if cmd == "cancel":
        return cmd_cancel(rest)
    if cmd == "any-running":
        return cmd_any_running(rest)
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
