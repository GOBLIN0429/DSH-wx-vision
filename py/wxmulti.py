#!/usr/bin/env python3
"""WeChat watcher for several groups.

Design (after the naive version failed):

  * the chat list is hashed by wxwatch alongside the conversation, so a change
    THERE is the signal that some conversation received a message — no blind
    cycling, no clicking when nothing happened;
  * WeChat floats a conversation that just received a message to the top of the
    list, so after a list change the group to open is simply the first row;
  * only then is the row clicked, the conversation read, and an @ answered.

Exactly one wxwatch is ever started (by this process). Two streams on the same
window fight over ScreenCaptureKit and the loser goes silent — which is how an
earlier single-group bridge stopped seeing anything.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
sys.path.insert(0, str(HERE))

import wxbridge as wb
import wxread
import wxsend

STATE = Path("/tmp/wxmulti_state.json")
LOG = Path("/tmp/wxmulti.log")
UI_NOISE = wxread.UI_NOISE | {"发送", "按住鼠标", "语音输入文字"}


def log(rec):
    rec["ts"] = time.strftime("%H:%M:%S")
    line = json.dumps(rec, ensure_ascii=False)
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(f"[multi] {line}", flush=True)


def norm(t):
    return wb.norm(t)


def matches(name, group):
    """Does a chat-list entry belong to `group`? Case-insensitive: the list may
    render a one-letter name as "Z" while the config says "z"."""
    n, g = name.strip().lower(), group.strip().lower()
    return g in n or n.startswith(g)


def parse_groups(raw_lines):
    """Parse groups.txt into [(name, reply_to_all)].

    A line is either a chat name, or `name|all`. `all` means an open
    conversation — a private chat — where every message deserves an answer
    rather than only those that mention us.
    """
    out = []
    for line in raw_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            name, _, mode = line.partition("|")
            out.append((name.strip(), mode.strip().lower() == "all"))
        else:
            out.append((line, False))
    return out


def load_state():
    try:
        return {k: set(v) for k, v in json.loads(STATE.read_text()).items()}
    except Exception:
        return {}


def save_state(state):
    STATE.write_text(json.dumps({k: sorted(v)[-400:] for k, v in state.items()},
                                ensure_ascii=False))


def frame():
    """Capture the window and return (image size, conv_left, width)."""
    from PIL import Image
    wid, rect = wxsend.window()
    if wid is None or not wxsend.capture(wid, wxsend.CAP):
        return None, None, None
    W, H = Image.open(wxsend.CAP).size
    conv_left = wb.detect_panel_left(str(wxsend.CAP))
    return (W, H), conv_left, rect


def chat_rows(conv_left):
    """Chat-list entries as dicts: name, y, x, preview.

    A chat entry is two lines — the name with a timestamp, then the message
    preview. OCR emits them as separate rows, so cluster rows that sit within
    100px of each other: the cluster's first line is the name, the rest is the
    preview. The preview is what carries WeChat's "[有人@我]" flag.
    """
    from PIL import Image
    im = Image.open(wxsend.CAP)
    im_h = im.height
    im.crop((0, 0, conv_left, im_h)).save("/tmp/list.png")
    raw = wxsend.ocr_tokens(Path("/tmp/list.png"))
    items = [t for t in raw if t["c"] >= 0.3 and t["t"].strip() not in UI_NOISE]

    cand = []
    for r in wxread.group_rows(items):
        text = r["text"].strip()
        if r["x"] < conv_left * 0.33 or not text:
            continue
        if r["y"] < im_h * 0.075 or text in ("搜索", "Search"):
            continue
        if text.isdigit() or (text.endswith("+") and text[:-1].isdigit()):
            continue                       # unread badge, not a row
        cand.append({"text": text, "y": r["y"], "x": r["x"]})
    cand.sort(key=lambda d: d["y"])

    clusters = []
    for c in cand:
        if clusters and c["y"] - clusters[-1][-1]["y"] < 100:
            clusters[-1].append(c)
        else:
            clusters.append([c])

    out = []
    name_edge = conv_left * 0.75      # right of this is the timestamp column
    for cl in clusters:
        # The timestamp shares the name's line and OCR may emit it first, which
        # made an entry show up named "15:47" with the real name pushed into the
        # preview. Split by column instead of by reading order.
        name_parts = [d for d in cl if d["x"] < name_edge]
        head = name_parts[0] if name_parts else cl[0]
        rest = [d for d in cl if d is not head]
        out.append({"name": head["text"], "y": head["y"], "x": head["x"],
                    "preview": " ".join(d["text"] for d in rest)})
    return out


def click_row(name, rows, rect, conv_left):
    """Click a chat row, using its name's y as the hit point."""
    hit = None
    for e in rows:
        if matches(e["name"], name):
            hit = e
            break
    if hit is None:
        return None
    sx = rect[0] + max(hit["x"], int(conv_left * 0.55)) // 2
    sy = rect[1] + (hit["y"] + 10) // 2
    subprocess.run([str(BIN / "wxtool"), "click", str(sx), str(sy)], capture_output=True)
    return hit["name"]


def header_title(conv_left):
    """Read the open conversation's title, to confirm a switch actually landed."""
    from PIL import Image
    im = Image.open(wxsend.CAP)
    im.crop((conv_left, 20, im.width, 120)).save("/tmp/hdr.png")
    raw = wxsend.ocr_tokens(Path("/tmp/hdr.png"))
    toks = sorted((t for t in raw if t["c"] >= 0.3), key=lambda t: t["x"])
    return " ".join(t["t"] for t in toks)


def read_conversation(conv_left, W, H):
    from PIL import Image
    if not Path(wxsend.CAP).exists():
        return []
    x0 = max(0, conv_left - 24)
    Image.open(wxsend.CAP).crop((x0, 0, W, H)).save("/tmp/multi_crop.png")
    raw = wxsend.ocr_tokens(Path("/tmp/multi_crop.png"))
    for t in raw:
        t["x"] += x0
    items = [t for t in raw if t["c"] >= 0.3 and t["t"].strip() not in UI_NOISE]
    rows = [r for r in wxread.group_rows(items)
            if r["text"].strip() and H * wb.HEADER_CUT <= r["y"] < H * wb.BOTTOM_CUT]
    return wb.classify_rows(rows, str(wxsend.CAP), conv_left)


def prime_group(group, state, names):
    """Mark a group's existing traffic as handled, except @s still owed an answer.

    Uses the same rule as the single-group bridge: a mention counts as handled
    only when our own reply appears below it. That keeps a restart of this
    watcher from re-answering yesterday's questions, while not dropping one that
    arrived while it was down.
    """
    f, conv_left, rect = frame()
    if f is None:
        return 0
    W, H = f
    rows = chat_rows(conv_left)
    opened = click_row(group, rows, rect, conv_left)
    if opened is None:
        log({"group": group, "stage": "prime-not-found"})
        return 0
    time.sleep(0.9)
    f, conv_left, rect = frame()
    if f is None:
        return 0
    W, H = f
    rows = read_conversation(conv_left, W, H)
    seen = state.setdefault(group, set())
    answered = [False] * len(rows)
    flag = False
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("kind") == "mine":
            flag = True
        answered[i] = flag
    kept = 0
    for i, r in enumerate(rows):
        is_mention = any(n.lower() in r["text"].lower() for n in names)
        if is_mention and not answered[i]:
            kept += 1
            continue
        seen.add(norm(r["text"]))
    save_state(state)
    log({"group": group, "stage": "primed", "rows": len(rows), "pending": kept})
    return kept


def current_group(conv_left, groups):
    """Which watched group is currently displayed, from the header title."""
    title = header_title(conv_left)
    for g in groups:
        if matches(title, g):
            return g
    return None


def has_owed(group, state, names, conv_left, W, H, require_mention=True):
    """Cheap check: does the open conversation contain a mention we owe?"""
    rows = read_conversation(conv_left, W, H)
    if not rows:
        return False
    answered = [False] * len(rows)
    flag = False
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("kind") == "mine":
            flag = True
        answered[i] = flag
    seen = state.get(group, set())
    mine = wb.sent_texts()
    for i, r in enumerate(rows):
        if r.get("kind") != "incoming" or answered[i]:
            continue
        text = r["text"].strip()
        if require_mention and not any(n.lower() in text.lower() for n in names):
            continue
        if wb.fuzzy_in(text, seen) or wb.fuzzy_in(text, mine):
            continue
        return True
    return False


def handle_open(group, state, harness, session_id, names, conv_left, W, H,
                require_mention=True):
    """Answer any mention in the ALREADY OPEN conversation that is still owed.

    The trigger for answering is positional, not a pre-built baseline: a mention
    counts as owed only when no message of OURS appears below it. That removes
    the need to sweep every group at startup just to learn what is already
    answered — which looked like the bot hopping between chats and leaving
    without saying anything.
    """
    seen = state.setdefault(group, set())
    rows = read_conversation(conv_left, W, H)
    if not rows:
        log({"group": group, "stage": "empty-read"})
        return
    paired = wb.pair_senders(rows, W, conv_left)
    sender_of = {}
    for q in paired:
        sender_of.setdefault(q["text"].strip(), q["sender"])

    answered_below = [False] * len(rows)
    flag = False
    for i in range(len(rows) - 1, -1, -1):
        if rows[i].get("kind") == "mine":
            flag = True
        answered_below[i] = flag

    mine = wb.sent_texts()
    targets = []
    for i, r in enumerate(rows):
        if r.get("kind") != "incoming":
            continue
        text = r["text"].strip()
        if not text:
            continue
        if require_mention and not any(n.lower() in text.lower() for n in names):
            continue
        if answered_below[i]:
            continue                      # we already replied below it
        if wb.fuzzy_in(text, seen) or wb.fuzzy_in(text, mine):
            continue                      # handled in this run, or it is ours
        targets.append(f"{sender_of.get(text, '未知')}: {text}")

    if not targets:
        log({"group": group, "stage": "no-owed-mention"})
        return
    # A private chat can accumulate several unanswered lines; answer the latest
    # ones instead of replying to an entire backlog in one breath.
    targets = targets[-2:]

    imgs = wb.detect_images(rows, str(wxsend.CAP), W, H, conv_left)
    paths = []
    for i, im_ in enumerate(imgs):
        box = wb.expand_box(im_["box"], W, H, conv_left)
        out = Path(f"/tmp/wx_image_{i}.png")
        try:
            from PIL import Image
            Image.open(wxsend.CAP).crop(box).save(out)
            paths.append(str(out))
        except Exception:
            pass

    ctx = [f'{q["sender"]}: {q["text"]}' for q in paired][-4:]
    prompt = wb.build_prompt(targets, ctx, paths).replace(
        "【微信群消息 → 需要你回复】", f"【微信群「{group}」→ 需要你回复】")
    try:
        r = harness.prompt(session_id, prompt)
        ok = (r.get("result") or {}).get("ok")
    except Exception as e:
        ok = f"{type(e).__name__}: {e}"
    if ok is True:
        for t in targets:
            seen.add(norm(t.split(": ", 1)[-1]))
        save_state(state)
    log({"group": group, "stage": "injected", "by": targets[-1][:50],
         "images": len(paths), "ok": ok})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", default="")
    ap.add_argument("--session", required=True)
    ap.add_argument("--names", default="GOBLIN,goblin")
    ap.add_argument("--settle", type=float, default=0.8)
    args = ap.parse_args()

    all_chats = set()
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    if not groups:
        # Chat names are Chinese, and passing them through a shell variable
        # mangles the bytes (observed: a variable that reads back correctly is
        # delivered to argv as "20"). Reading a UTF-8 file sidesteps the shell.
        cfg = HERE.parent / "groups.txt"
        if cfg.exists():
            parsed = parse_groups(cfg.read_text(encoding="utf-8").splitlines())
            groups = [g for g, _ in parsed]
            all_chats = {g for g, every in parsed if every}
            log({"stage": "config", "chats": parsed})
    if not groups:
        print("[multi] no groups configured (groups.txt)", flush=True)
        return 2
    names = [n.strip() for n in args.names.split(",") if n.strip()]
    state = load_state()

    harness = wb.Harness()
    if not harness.login():
        print("[multi] cannot authenticate", flush=True)
        return 1
    print(f"[multi] groups={groups}", flush=True)
    # Its own capture file: the agent answering a message runs wxsend.py, which
    # writes /tmp/wx_send.png. Sharing that path made this process open a file
    # another process had just replaced, crashing the watcher mid-sweep.
    wxsend.CAP = Path("/tmp/wxmulti_cap.png")
    wb.ensure_geometry()

    det = subprocess.Popen([str(BIN / "wxwatch")], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, bufsize=1)
    log({"stage": "watching", "groups": groups})
    try:
        for line in det.stdout:
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("ev") != "change":
                continue
            src = ev.get("src")

            f, conv_left, rect = frame()
            if f is None:
                continue
            W, H = f
            here = current_group(conv_left, groups)

            # ---- conversation changed: handle THIS chat in place, no clicking
            if src == "frame":
                if here is None:
                    continue
                handle_open(here, state, harness, args.session, names, conv_left, W, H,
                            here not in all_chats)
                continue

            # ---- list changed: another conversation moved. Finish the current
            # one first — replying out of order in a live chat is worse than a
            # second of delay on the other group.
            if src != "list":
                continue
            time.sleep(args.settle)
            here_all = here in all_chats
            if here is not None and has_owed(here, state, names, conv_left, W, H,
                                             not here_all):
                log({"stage": "current-first", "group": here})
                handle_open(here, state, harness, args.session, names, conv_left, W, H,
                            not here_all)
                f, conv_left, rect = frame()
                if f is None:
                    continue
                W, H = f
                if has_owed(here, state, names, conv_left, W, H, not here_all):
                    log({"stage": "still-owed", "group": here})
                    continue

            rows = chat_rows(conv_left)
            if not rows:
                log({"stage": "skip", "why": "empty list"})
                continue
            # WeChat floats the conversation that just received a message to the
            # top of the list, so the first entry IS the one to look at. Reading
            # the "[有人@我]" preview instead was tried and abandoned: OCR turns
            # it into noise like "$（0 -°）" and it can never be matched.
            top = rows[0]["name"]
            match = next((g for g in groups if matches(top, g)), None)
            if match is None:
                log({"stage": "skip", "why": "top not watched", "top": top[:30]})
                continue
            if match == here and not has_owed(match, state, names, conv_left, W, H,
                                              match not in all_chats):
                log({"stage": "skip", "why": "already open, nothing owed", "group": match})
                continue
            # The list can reorder between the OCR snapshot and the click, so a
            # stale y lands on a neighbour (WeChat Team, typically) and the bot
            # appears to hop around. Verify the title and retry once with fresh
            # coordinates instead of trusting the first click.
            opened = None
            for attempt in range(2):
                if attempt == 0:
                    target_rows = rows
                else:
                    f, conv_left, rect = frame()
                    if f is None:
                        break
                    W, H = f
                    target_rows = chat_rows(conv_left)
                if click_row(match, target_rows, rect, conv_left) is None:
                    break
                time.sleep(1.1)
                f, conv_left, rect = frame()
                if f is None:
                    break
                W, H = f
                opened = current_group(conv_left, groups)
                if opened == match:
                    break
                log({"stage": "switch-mismatch", "want": match,
                     "got": opened or header_title(conv_left)[:20],
                     "attempt": attempt + 1})
                opened = None
            if opened is None:
                log({"stage": "skip", "why": "switch failed after retry",
                     "want": match})
                continue
            handle_open(opened, state, harness, args.session, names, conv_left, W, H,
                        opened not in all_chats)
    finally:
        det.terminate()


if __name__ == "__main__":
    sys.exit(main())
