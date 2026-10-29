#!/usr/bin/env python3
"""WeChat send pipeline (pixel-driven).

WeChat 4.x exposes no internal UI through Accessibility (AXTextField/AXTextArea
count is zero), so every coordinate here comes from the rendered pixels:
  - the compose box is bounded above by a full-width separator line
  - the conversation panel's left edge comes from the OCR layout
  - the caret is placed by clicking, then text arrives via clipboard paste
    because synthetic key events cannot drive the Chinese IME.

Safety: --dry-run stops before pressing Return, so the draft can be inspected.
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
TOOL = BIN / "wxtool"
ACTIVATE = BIN / "activate"
WPID = BIN / "wpid"
OCRJSON = BIN / "ocrjson"
WINLIST = BIN / "winlist"
WXTOOL = str(TOOL)
CAP = Path("/tmp/wx_send.png")


def wechat_pid():
    out = subprocess.run([str(WPID), "微信"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "pid=" in line:
            return int(line.split("pid=")[1].split()[0])
    return None


def front_is_wechat():
    front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True).stdout.strip()
    if not front:
        return False
    info = subprocess.run(["lsappinfo", "info", "-only", "name", front],
                          capture_output=True, text=True).stdout
    return "微信" in info


def ensure_front(retries=3):
    """Re-assert focus immediately before each synthetic input.

    Anything else that grabs focus between our activate() and the keystroke
    (our own tooling, a notification, the user) silently reroutes Cmd+V and
    Return to the wrong app.
    """
    for _ in range(retries):
        if front_is_wechat():
            return True
        activate()
        time.sleep(0.3)
    return front_is_wechat()


def activate():
    """Bring WeChat to the front.

    Synthetic Cmd+V goes to whatever app is frontmost, and DSH is frontmost
    while it drives this pipeline — without this the paste lands in DSH's own
    input box instead of WeChat's.
    """
    pid = wechat_pid()
    if pid is None:
        return False
    subprocess.run([str(ACTIVATE), str(pid)], capture_output=True)
    time.sleep(0.6)
    return True


def window(min_width=200):
    """Pick the LARGEST WeChat window.

    WeChat keeps small helper windows around (a 66x20 tooltip appears while
    typing), and they sort ahead of the main window, so taking the first match
    captures a sliver and every derived coordinate is then wrong.
    """
    out = subprocess.run([str(WINLIST)], capture_output=True, text=True).stdout
    best = (None, None, -1)
    for line in out.splitlines():
        if 'owner="微信"' not in line:
            continue
        try:
            wid = int(line.split("id=")[1].split()[0])
            rect = tuple(int(v) for v in line.split("rect=")[1].split(","))
        except Exception:
            continue
        if rect[2] < min_width:
            continue
        area = rect[2] * rect[3]
        if area > best[2]:
            best = (wid, rect, area)
    return best[0], best[1]


def capture(wid, path=CAP):
    if path.exists():
        path.unlink()
    subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), str(path)],
                   capture_output=True)
    return path.exists()


def ocr_tokens(path):
    import json
    r = subprocess.run([str(OCRJSON), str(path)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "[]")
    except Exception:
        return []


def _panel_left(im, W, H):
    """x of the chat-list / conversation divider, in image pixels.

    Mirrors the bridge's rule: scan a 1/4-scale copy for a near-uniform column
    whose left and right backgrounds differ, keep the RIGHTMOST such column in
    the left half, and reject results outside a plausible band. Everything
    downstream — including where the click lands — depends on this.
    """
    scale = 4
    small = im.resize((max(1, W // scale), max(1, H // scale)))
    sw, sh = small.size
    px = small.load()
    y0, y1 = int(sh * 0.07), int(sh * 0.78)
    ys = list(range(y0, y1, 4))
    best = None
    if ys:
        for x in range(int(sw * 0.04), int(sw * 0.45)):
            vals = [sum(px[x, y]) / 3 for y in ys]
            m = sum(vals) / len(vals)
            sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
            if sd >= 3.0:
                continue
            lm = sum(sum(px[max(0, x - 2), y]) / 3 for y in ys) / len(ys)
            rm = sum(sum(px[min(sw - 1, x + 2), y]) / 3 for y in ys) / len(ys)
            if abs(lm - rm) > 2.5:
                best = x
    lo, hi, expect = 0.28, 0.40, 0.357
    if best is None or not (lo <= best / sw <= hi):
        return int(W * expect)
    return best * scale


def compose_box(img_path):
    """Locate the compose box.

    Returns (click_point_points, click_point_px, info, text_rect).

    ``text_rect`` is the crop that holds ONLY the draft text — verification
    must never read the whole window, because WeChat's own conversation may
    already contain the reply string (a previous "@me 13" made an unscoped
    check report success for a message that was never sent).
    """
    from PIL import Image
    im = Image.open(img_path).convert("RGB")
    W, H = im.size
    px = im.load()

    # 1) full-width separator line above the compose area
    border = None
    for y in range(int(H * 0.65), H):
        bright = sum(1 for x in range(0, W, 4) if sum(px[x, y]) / 3 > 45)
        if bright > (W / 4) * 0.8:
            border = y
            break

    # 2) conversation left edge.
    #
    #    This used to be a flat 0.10*W, measured when the chat list was collapsed
    #    to its icon rail. With the list expanded it occupies roughly 0.36*W, so
    #    a 0.10*W "compose box" sat INSIDE the chat list — clicking it opened
    #    whichever conversation was under the cursor (WeChat Team in practice) and
    #    the bot looked like it was hopping between chats. Detect it instead.
    conv_left = _panel_left(im, W, H)

    if border is None:
        border = int(H * 0.82)

    # interior of the box sits just below the border, above the toolbar icons
    y_px = border + int(H * 0.045)
    x_px = (conv_left + int(W * 0.97)) // 2

    text_rect = (
        conv_left + int(W * 0.02),          # x0
        border + 4,                          # y0
        int(W * 0.97),                       # x1
        border + int(H * 0.11),              # y1 — stays above the icon row
    )
    return (x_px // 2, y_px // 2), (x_px, y_px), (W, H, border, conv_left), text_rect


def ocr_region(img_path, rect, scale=2):
    """OCR a crop only, so chrome elsewhere cannot satisfy the check."""
    from PIL import Image
    im = Image.open(img_path)
    x0, y0, x1, y1 = rect
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(im.width, x1); y1 = min(im.height, y1)
    if x1 <= x0 or y1 <= y0:
        return ""
    crop = im.crop((x0, y0, x1, y1))
    if scale != 1:
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
    tmp = Path("/tmp/wx_region.png")
    crop.save(tmp)
    toks = [t["t"] for t in ocr_tokens(tmp) if t["c"] >= 0.3]
    return "".join(toks)


def normalize(s):
    return "".join(s.split())


def set_clipboard(text):
    p = subprocess.run(["pbcopy"], input=text.encode("utf-8"))
    return p.returncode == 0


def snapshot_text(path=None):
    path = path or Path("/tmp/wx_after.png")
    wid, _ = window()
    capture(wid, path)
    toks = [t["t"] for t in ocr_tokens(path) if t["c"] >= 0.3]
    return " ".join(toks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    wid, rect = window()
    if wid is None:
        print("ERROR: 微信窗口未找到")
        return 1
    if not capture(wid):
        print("ERROR: 截图失败")
        return 1

    (cx, cy), (x_px, y_px), info, text_rect = compose_box(CAP)
    W, H, border, conv_left = info

    # compose_box works in the captured image's own pixel space. Synthetic
    # mouse events are in SCREEN points, so the window origin must be added —
    # without it the click lands on whatever window occupies those coordinates
    # (here: DSH itself), which steals focus back and sends Cmd+V to the wrong app.
    wx, wy = rect[0], rect[1]
    screen_x, screen_y = wx + cx, wy + cy
    print(f"window origin=({wx},{wy}) rect={rect} px={W}x{H} border_y={border} conv_left={conv_left}")
    print(f"click: window-relative pt=({cx},{cy})  ->  screen pt=({screen_x},{screen_y})")
    print(f"verify rect: {text_rect}")

    print("→ 激活微信窗口")
    if not activate():
        print("ERROR: 无法激活微信")
        return 1

    print("→ 点击输入框")
    subprocess.run([WXTOOL, "click", str(screen_x), str(screen_y)], capture_output=True)
    time.sleep(0.35)

    print(f"→ 写剪贴板: {args.text[:40]!r}")
    if not set_clipboard(args.text):
        print("ERROR: pbcopy 失败")
        return 1

    print("→ Cmd+V 粘贴")
    if not ensure_front():
        print("WARN: 微信不是前台，粘贴可能落空")
    subprocess.run([WXTOOL, "paste"], capture_output=True)
    time.sleep(0.45)

    capture(wid, Path("/tmp/wx_draft.png"))
    scoped = normalize(ocr_region(Path("/tmp/wx_draft.png"), text_rect))
    appeared = normalize(args.text) in scoped
    print(f"→ 草稿校验(仅输入框): {'✅ ' + scoped[:40] if appeared else '⚠️ 输入框内未见文字 → ' + (scoped[:40] or '<空>')}")

    if not appeared:
        print("草稿未确认，放弃发送。截图: /tmp/wx_draft.png")
        return 2

    if args.dry_run:
        print("dry-run：已停在发送前，未按回车。截图: /tmp/wx_draft.png")
        return 0

    print("→ 回车发送")
    if not ensure_front():
        print("WARN: 微信不是前台，回车可能落空")
    subprocess.run([WXTOOL, "enter"], capture_output=True)
    time.sleep(0.7)

    # Sent means: the draft left the input box. Conversation-side evidence is
    # deliberately not used — an identical string may already exist there.
    capture(wid, Path("/tmp/wx_sent.png"))
    after_box = normalize(ocr_region(Path("/tmp/wx_sent.png"), text_rect))
    cleared = normalize(args.text) not in after_box
    print(f"→ 发送校验(输入框已清空): {'✅ 已送出' if cleared else '⚠️ 文字仍留在输入框，可能未发送'}")
    if cleared:
        # The bridge reads this so a long outgoing bubble whose left edge drifts
        # left is never mistaken for an incoming message.
        try:
            import json as _json
            from pathlib import Path as _P
            log = _P("/tmp/wx_sent_texts.json")
            items = _json.loads(log.read_text()) if log.exists() else []
            items.append(args.text)
            log.write_text(_json.dumps(items[-200:], ensure_ascii=False))
        except Exception:
            pass
    print("截图: /tmp/wx_sent.png")
    return 0 if cleared else 3

    wid2, _ = window()
    capture(wid2, Path("/tmp/wx_sent.png"))
    after = " ".join(t["t"] for t in ocr_tokens(Path("/tmp/wx_sent.png")) if t["c"] >= 0.3)
    print(f"→ 发送后校验: {'✅ 消息已出现在会话中' if args.text[:8] in after.replace(' ','') else '⚠️ 未检出'}")
    print("截图: /tmp/wx_sent.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
