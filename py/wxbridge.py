#!/usr/bin/env python3
"""WeChat <-> DSH session bridge.

Instead of calling a model with a one-line system prompt, this injects the
incoming WeChat message into a real DSH session (the one the user is talking
to), so the reply comes from the full agent: its context, tools, memory and
installed plugins.

Transport, reverse-engineered from the harness itself:
    GET  /?token=<token>            -> sets the session cookie
    POST /api/<endpoint>            -> {"type":"client-request","payload":{"args":...}}

Detection still comes from wxwatch (frame-hash + unread-count); replying is the
agent's job, via wxsend.py.
"""
import argparse
import difflib
import http.cookiejar
import os
import signal
import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
sys.path.insert(0, str(HERE))

import wxread
import wxsend

BASE = "http://127.0.0.1:43129"
LOG = Path.home() / "Library/Logs/DSH Desktop/harness.log"
JAR = Path("/tmp/wxbridge.jar")
SEEN = Path("/tmp/wxbridge_seen.json")
SENT = Path("/tmp/wx_sent_texts.json")
TIMING = Path("/tmp/wxbridge_timing.log")
UI_NOISE = wxread.UI_NOISE | {"发送", "按住鼠标", "语音输入文字"}
HEADER_CUT, BOTTOM_CUT = 0.12, 0.80


def now_ms():
    return int(time.time() * 1000)


def log_event(rec):
    rec["ts"] = time.strftime("%H:%M:%S")
    with TIMING.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[bridge] {json.dumps(rec, ensure_ascii=False)}", flush=True)


def token():
    try:
        m = re.findall(r"token=([A-Za-z0-9_-]+)", LOG.read_text(errors="replace"))
        return m[-1] if m else None
    except Exception:
        return None


class Harness:
    """Minimal client for the harness JSON-RPC-over-HTTP surface."""

    def __init__(self):
        self.jar = http.cookiejar.MozillaCookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPRedirectHandler(),
        )

    def login(self):
        t = token()
        if not t:
            return False
        try:
            self.opener.open(f"{BASE}/?token={t}", timeout=10).read()
            return True
        except Exception as e:
            print(f"[bridge] login failed: {e}", flush=True)
            return False

    def call(self, endpoint, args, timeout=30):
        body = json.dumps({
            "type": "client-request",
            "rpcId": str(uuid.uuid4()),
            "method": endpoint,
            "payload": {"args": args},
        }).encode()
        req = urllib.request.Request(
            f"{BASE}/api/{endpoint}", data=body,
            headers={"content-type": "application/json"})
        with self.opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())

    def sessions(self):
        d = self.call("session/list", {"_request": {}})
        return (d.get("result", {}).get("value") or {}).get("items") or []

    def prompt(self, session_id, text, images=None):
        content = [{"type": "text", "text": text}]
        for b64 in (images or []):
            content.append({"type": "image", "mediaType": "image/png",
                            "data": b64, "name": "wechat.png"})
        return self.call("session/prompt", {
            "request": {
                "requestId": str(uuid.uuid4()),
                "sessionId": session_id,
                "mode": "queue",
                "content": content,
            }
        }, timeout=60)


def fuzzy_in(text, keys, cutoff=None):
    """OCR re-reads the same bubble slightly differently (drops the @, swaps
    几/儿). Exact keys miss those, so compare fuzzily as well — otherwise an
    already-answered message gets injected again and the group sees a repeat."""
    n = norm(text)
    if cutoff is None:
        # Short messages need a much tighter test. "我叫什么" and "那我叫什么"
        # score 0.957 against each other, so the loose cutoff silently swallowed
        # a genuine new question as "already answered". Two characters of
        # difference in a short line matters; in a long one it is OCR jitter.
        cutoff = 0.85 if len(n) >= 16 else 0.98
    if n in keys:
        return True
    for k in keys:
        if abs(len(k) - len(n)) > max(6, len(n) * 0.5):
            continue
        if difflib.SequenceMatcher(None, n, k).ratio() >= cutoff:
            return True
    return False


def sent_texts():
    try:
        return {norm(x) for x in json.loads(SENT.read_text())}
    except Exception:
        return set()


def norm(t):
    return "".join(t.translate(str.maketrans({"（": "(", "）": ")", "：": ":", "，": ",",
                                              "？": "?", "！": "!", "　": " "})).split()).lower()


GEOM = BIN / "wxgeom"
_last_H = 0    # height of the frame most recently read by read_conv()
_winc = {"wid": None, "rect": None, "at": 0.0}


def ensure_geometry():
    """Force the WeChat window back to the canonical frame.

    Every coordinate downstream (click point, OCR panel crop, hashing region) is
    derived from the live window rect, so a dragged or resized window silently
    shifts all of them. wxgeom also self-corrects, so calling it periodically
    is enough to keep the geometry pinned.
    """
    try:
        r = subprocess.run([str(GEOM)], capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout) if r.stdout.strip() else {}
        if d.get("adjusted"):
            print(f"[bridge] 窗口几何已纠正: {d.get('before')} -> {d.get('after')}", flush=True)
        elif d and not d.get("ok"):
            print(f"[bridge] 窗口几何未能纠正: {d}", flush=True)
        return bool(d.get("ok"))
    except Exception as e:
        print(f"[bridge] 几何校验失败: {type(e).__name__}: {e}", flush=True)
        return False


def read_conv():
    now = time.time()
    if _winc["wid"] is None or now - _winc["at"] > 30:
        # Passively re-check geometry on the refresh tick; wxgeom corrects it if
        # the window drifted, and every derived coordinate follows from it.
        ensure_geometry()
        wid, rect = wxsend.window()
        if wid is not None:
            _winc.update({"wid": wid, "rect": rect, "at": now})
    wid = _winc["wid"]
    if wid is None or not wxsend.capture(wid, Path("/tmp/wxbridge_cap.png")):
        return None, []
    from PIL import Image
    im = Image.open("/tmp/wxbridge_cap.png")
    W, H = im.size
    conv_left = detect_panel_left("/tmp/wxbridge_cap.png")
    x0 = max(0, conv_left - 24)          # keep a little margin before the divider
    im.crop((x0, 0, W, H)).save("/tmp/wxbridge_crop.png")
    raw = wxsend.ocr_tokens(Path("/tmp/wxbridge_crop.png"))
    for t in raw:
        t["x"] += x0
    items = [t for t in raw if t["c"] >= 0.3 and t["t"].strip() not in UI_NOISE]
    rows = [r for r in wxread.group_rows(items)
            if r["text"].strip() and H * HEADER_CUT <= r["y"] < H * BOTTOM_CUT]
    rows = classify_rows(rows, "/tmp/wxbridge_cap.png", conv_left)
    global _last_H
    _last_H = H
    return W, rows, conv_left


NICK_REL = 0.13   # width fraction right of the divider that holds nicknames
MINE_REL = 0.55   # beyond this fraction of the panel, a bubble is ours


def detect_panel_left(img_path, W=None, H=None):
    """Find the divider between the chat list and the conversation.

    Scans a 1/4-scale copy: the check is per-column statistics, and doing it at
    full resolution cost ~2.5 s per read for no extra accuracy. Returns the
    divider x in FULL-image pixels.
    """
    from PIL import Image
    im = Image.open(img_path).convert("RGB")
    full_w, full_h = im.size
    scale = 4
    small = im.resize((max(1, full_w // scale), max(1, full_h // scale)))
    sw, sh = small.size
    px = small.load()
    y0, y1 = int(sh * 0.07), int(sh * 0.78)
    ys = list(range(y0, y1, 4))
    if not ys:
        return int(full_w * 0.34)
    # Start well left of the usual divider: with the chat list collapsed to its
    # icon rail the edge sits around 9% of the width, and a scan starting at 20%
    # would miss it and fall back to a wrong guess.
    # Search only the left half and keep the RIGHTMOST candidate: the divider is
    # the chat list's right boundary. The icon rail's edge sits further left and
    # has a BIGGER step (52 vs 17), so "strongest" picks the rail and ends up
    # reading the chat list as conversation; scanning past 45% instead picks up
    # edges inside the conversation (a photo's border once won at 63%).
    best = None
    best_delta = 0.0
    for x in range(int(sw * 0.04), int(sw * 0.45)):
        vals = [sum(px[x, y]) / 3 for y in ys]
        m = sum(vals) / len(vals)
        sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5
        if sd >= 3.0:
            continue
        lm = sum(sum(px[max(0, x - 2), y]) / 3 for y in ys) / len(ys)
        rm = sum(sum(px[min(sw - 1, x + 2), y]) / 3 for y in ys) / len(ys)
        delta = abs(lm - rm)
        if delta > 2.5:
            best = x          # rightmost wins, not strongest
    # Sanity clamp. The divider is the chat list's right edge and lands around
    # 0.35 of the window width; content-dependent edges (a photo border, the icon
    # rail) can win the scan and return 0.08 or 0.45, which silently turns the
    # chat list into "conversation". Anything outside a plausible band is
    # rejected in favour of the known-good ratio.
    lo, hi, expect = 0.28, 0.40, 0.357
    if best is None:
        return int(full_w * expect)
    ratio = best / sw
    if not (lo <= ratio <= hi):
        return int(full_w * expect)
    return best * scale


def read_conv():
    now = time.time()
    if _winc["wid"] is None or now - _winc["at"] > 30:
        # Passively re-check geometry on the refresh tick; wxgeom corrects it if
        # the window drifted, and every derived coordinate follows from it.
        ensure_geometry()
        wid, rect = wxsend.window()
        if wid is not None:
            _winc.update({"wid": wid, "rect": rect, "at": now})
    wid = _winc["wid"]
    if wid is None or not wxsend.capture(wid, Path("/tmp/wxbridge_cap.png")):
        return None, []
    from PIL import Image
    im = Image.open("/tmp/wxbridge_cap.png")
    W, H = im.size
    conv_left = detect_panel_left("/tmp/wxbridge_cap.png")
    x0 = max(0, conv_left - 24)          # keep a little margin before the divider
    im.crop((x0, 0, W, H)).save("/tmp/wxbridge_crop.png")
    raw = wxsend.ocr_tokens(Path("/tmp/wxbridge_crop.png"))
    for t in raw:
        t["x"] += x0
    items = [t for t in raw if t["c"] >= 0.3 and t["t"].strip() not in UI_NOISE]
    rows = [r for r in wxread.group_rows(items)
            if r["text"].strip() and H * HEADER_CUT <= r["y"] < H * BOTTOM_CUT]
    rows = classify_rows(rows, "/tmp/wxbridge_cap.png", conv_left)
    global _last_H
    _last_H = H
    return W, rows, conv_left


NICK_REL = 0.13   # width fraction right of the divider that holds nicknames
MINE_REL = 0.55   # beyond this fraction of the panel, a bubble is ours


VOICE_NOISE = ("转文字", "按住", "语音输入", "秒)")


def _is_ui_noise(text):
    """Voice messages and widget chrome OCR as text and must not be answered."""
    t = text.strip()
    if any(k in t for k in VOICE_NOISE):
        return True
    if len(t) <= 3 and t.replace('"', '').replace("'", "").isdigit():
        return True          # a voice clip's duration, e.g. 2"
    return False


def classify_rows(rows, img_path, conv_left):
    """Label each row by what sits BEHIND its text.

    Position cannot separate a nickname from its bubble: WeChat left-aligns the
    nickname with the bubble, so they differ by only ~15pt. The bubble's own
    background can: panel background is flat, an incoming bubble is a lighter
    grey, and our own bubble is saturated green. Measured on this layout:
        panel 30, incoming bubble 47, outgoing bubble 134
    So sample just left of the text and compare with the panel background at
    that row.
    """
    from PIL import Image
    im = Image.open(img_path).convert("RGB")
    px = im.load()
    W, H = im.size
    for r in rows:
        y = min(H - 1, r["y"] + 12)
        # Background at this row: the darkest of a few spread samples beats a
        # full sweep (which cost ~1 s per frame) at identical accuracy.
        bg = 255.0
        panel = W - conv_left
        for frac in (0.01, 0.22, 0.45, 0.68, 0.90, 0.98):
            x = int(conv_left + panel * frac)
            if x < 0 or x >= W:
                continue
            v = sum(px[x, y]) / 3
            if v < bg:
                bg = v
        lx = max(conv_left + 1, r["x"] - 8)
        lb = sum(px[lx, y]) / 3
        if lb < bg + 6:
            r["kind"] = "chrome"        # nickname or timestamp, no bubble
        elif lb < bg + 60:
            r["kind"] = "incoming"
        else:
            r["kind"] = "mine"
    return rows


def pair_senders(rows, W, conv_left, merge_gap=80):
    """Attach a sender to every incoming bubble and rejoin wrapped lines.

    WeChat prints the nickname above a bubble and only for the first message of
    a consecutive run, so carry the last nickname forward. A long message wraps
    into several OCR rows; same-sender rows within ``merge_gap`` are one message
    and join with no separator because Chinese wraps without a space.
    """
    panel = max(1, W - conv_left)
    out = []
    sender = None
    for r in rows:
        text = r["text"].strip()
        kind = r.get("kind", "incoming")
        if kind == "chrome":
            rel = (r["x"] - conv_left) / panel
            # Left-aligned chrome is a nickname; centred chrome is a timestamp.
            if rel < 0.35 and text and len(text) <= 16:
                sender = text
            continue
        if kind == "mine":
            continue
        if not text or _is_ui_noise(text):
            continue
        who = sender or "未知"
        if out and out[-1]["sender"] == who and (r["y"] - out[-1]["y"]) <= merge_gap:
            out[-1]["text"] += text
            out[-1]["y"] = r["y"]
            continue
        out.append({"sender": who, "text": text, "x": r["x"], "y": r["y"]})
    return out


GAP_MIN = 180      # px of text-free vertical space that only a picture leaves
FILL_MIN = 5.0     # band must not be flat background
PAD = 8


IMG_PAD = 0.5    # expand a detected picture box by this fraction each way


def expand_box(box, W, H, conv_left, pad=IMG_PAD):
    """Grow a detected picture box before cropping.

    The detector finds the strongly-coloured core of a picture; a screenshot
    with pale margins (a phone capture on a light background, say) gets found
    only in part. Expanding generously costs nothing and prevents cropping a
    caption or the board's edge off.
    """
    x0, y0, x1, y1 = [int(v) for v in box]
    w, h = x1 - x0, y1 - y0
    return (max(conv_left, int(x0 - w * pad)), max(0, int(y0 - h * pad)),
            min(W, int(x1 + w * pad)), min(H, int(y1 + h * pad)))


def crop_image_b64(box, img_path, max_side=1000):
    """Crop one picture bubble and return it as base64 PNG.

    Downscaled to at most ``max_side``: the bubble can be a full-width photo and
    the payload rides inside a JSON-RPC body, so keep it modest — a vision model
    does not need more than this to read a chat picture.
    """
    import base64
    import io
    from PIL import Image
    im = Image.open(img_path).convert("RGB")
    x0, y0, x1, y1 = [int(v) for v in box]
    x0 = max(0, x0); y0 = max(0, y0)
    x1 = min(im.width, x1); y1 = min(im.height, y1)
    if x1 <= x0 or y1 <= y0:
        return None
    crop = im.crop((x0, y0, x1, y1))
    if max(crop.width, crop.height) > max_side:
        scale = max_side / max(crop.width, crop.height)
        crop = crop.resize((max(1, int(crop.width * scale)),
                            max(1, int(crop.height * scale))))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def detect_images(rows, img_path, W, H, conv_left, panel_bottom=None):
    """Find picture bubbles by the hole they leave in the text flow.

    Colour statistics cannot separate a photo from a bubble: both are "colourful"
    next to flat background, and both score high on variance. What IS unique to a
    picture is that OCR produces no text inside it, so it opens a vertical gap far
    larger than any message spacing (measured: messages gap 54-127px). So look for
    big text-free bands that are not flat background, and take their colourful
    extent — that is the picture.
    """
    from PIL import Image
    im = Image.open(img_path).convert("RGB")
    px = im.load()
    top = int(H * HEADER_CUT)
    bottom = panel_bottom or int(H * BOTTOM_CUT)

    ys = sorted({r["y"] for r in rows})
    bands = []
    edges = [top] + ys + [bottom]
    for a, b in zip(edges, edges[1:]):
        if b - a >= GAP_MIN:
            bands.append((a + PAD, b - PAD))

    def colourful(x0, y0, x1, y1):
        vals = []
        for y in range(max(0, y0), min(H, y1), 6):
            for x in range(max(conv_left, x0), min(W, x1), 6):
                r, g, bl = px[x, y]
                vals.append(max(r, g, bl) - min(r, g, bl))
        return (sum(vals) / len(vals)) if vals else 0.0

    found = []
    for y0, y1 in bands:
        if y1 - y0 < GAP_MIN * 0.5:
            continue
        if colourful(conv_left, y0, W, y1) < FILL_MIN:
            continue
        # trim to the colourful extent so padding/背景 does not inflate the crop
        left, right, up, down = W, conv_left, y1, y0
        for y in range(y0, y1, 4):
            for x in range(conv_left, W, 4):
                r, g, bl = px[x, y]
                if max(r, g, bl) - min(r, g, bl) > FILL_MIN:
                    left = min(left, x); right = max(right, x)
                    up = min(up, y); down = max(down, y)
        if right - left < 60 or down - up < 60:
            continue
        found.append({"box": [left, up, right + 1, down + 1],
                      "x": left, "y": up, "w": right - left + 1, "h": down - up + 1})
    return found


CONTEXT_LINES = 3


def build_prompt(heard, ctx, image_paths=None):
    """Keep the injected prompt thin — the persona lives in AGENTS.md, which
    the target session loads from its own cwd. Duplicating it here would give
    two sources of truth that drift apart."""
    lines = ["【微信群消息 → 需要你回复】", ""]
    if ctx:
        lines.append("刚才群里：")
        for c in ctx[-CONTEXT_LINES:]:
            lines.append("  " + c)
        lines.append("")
    lines.append("现在 @ 你的消息（冒号前是发言人微信名）：")
    for h in heard:
        lines.append("  " + h)
    lines.append("")
    if image_paths:
        lines.append(f"这条消息带了 {len(image_paths)} 张图片，已存到本机：")
        for path in image_paths:
            lines.append(f"  {path}")
        lines.append("**先用 read_image 工具看清楚图片内容，再决定怎么回。**")
    lines.append("按本工作区 AGENTS.md 的角色设定来回复，注意区分主人和别人。")
    lines.append("用这条命令发回群里（只发一条）：")
    lines.append(f'  python3 "{HERE}/wxsend.py" --text "你要说的话"')
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None, help="target DSH session id")
    ap.add_argument("--names", default="GOBLIN,goblin")
    ap.add_argument("--settle-ms", type=int, default=600)
    args = ap.parse_args()
    names = [n.strip() for n in args.names.split(",") if n.strip()]

    h = Harness()
    if not h.login():
        print("[bridge] cannot authenticate to harness", flush=True)
        return 1

    session_id = args.session
    if not session_id:
        items = h.sessions()
        items.sort(key=lambda x: x.get("updatedAt", 0), reverse=True)
        if not items:
            print("[bridge] no sessions", flush=True)
            return 1
        session_id = items[0]["sessionId"]
    STATE = Path("/tmp/wxbridge.state")
    STATE.write_text(json.dumps({"session_id": session_id, "pid": os.getpid()}))
    print(f"[bridge] target session: {session_id}", flush=True)

    try:
        seen = set(json.loads(SEEN.read_text()))
    except Exception:
        seen = set()

    ensure_geometry()
    W0, rows0, _cl0 = read_conv()
    if rows0:
        # Baseline rule: a mention counts as handled only if OUR reply already
        # appears below it. Marking everything seen drops questions that arrived
        # just before a restart; marking nothing seen re-answers old ones. The
        # reply's position is what distinguishes the two.
        pending_at_start = 0
        answered_after = [False] * len(rows0)
        seen_reply = False
        for i in range(len(rows0) - 1, -1, -1):
            kind = rows0[i].get("kind", "incoming")
            if kind == "mine":
                seen_reply = True
            answered_after[i] = seen_reply
        for i, r in enumerate(rows0):
            text = r["text"]
            is_mention = any(n.lower() in text.lower() for n in names)
            if is_mention and not answered_after[i]:
                pending_at_start += 1
                continue                      # still owed an answer
            seen.add(norm(text))
    print(f"[bridge] baseline {len(seen)} rows", flush=True)

    def _bye(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    det = subprocess.Popen([str(BIN / "wxwatch")], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, bufsize=1)

    if pending_at_start:
        # Messages that arrived while the bridge was down leave no change event
        # for the detector to fire on, so kick one cycle ourselves.
        print(f"[bridge] {pending_at_start} 条未回复的 @ 待处理，立即补一轮", flush=True)
        det.events.put({"ev": "change", "src": "startup"})
    context = [r["text"] for r in (rows0 or [])][-20:]

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

            time.sleep(args.settle_ms / 1000.0)
            t0 = now_ms()
            W, rows, conv_left = read_conv()
            t_read = now_ms()
            if W is None:
                continue
            # Senders first: the persona has to know whether a message came
            # from 主人 or from someone else, so every injected line carries a
            # name. pair_senders also drops our own right-aligned bubbles.
            paired = pair_senders(rows, W, conv_left)
            context = (context + [f'{p["sender"]}: {p["text"]}' for p in paired])[-40:]

            mine = sent_texts()
            fresh = [p for p in paired
                     if not fuzzy_in(p["text"], seen) and not fuzzy_in(p["text"], mine)]
            for p in fresh:
                seen.add(norm(p["text"]))
            SEEN.write_text(json.dumps(sorted(seen)[-500:], ensure_ascii=False))

            targets = [f'{p["sender"]}: {p["text"]}' for p in fresh
                       if any(n.lower() in p["text"].lower() for n in names)]
            if not targets:
                if fresh:
                    log_event({"stage": "read-only", "count": len(fresh),
                               "last": f'{fresh[-1]["sender"]}: {fresh[-1]["text"][:40]}',
                               "read_ms": t_read - t0})
                continue

            # Pictures carry no text, so they never appear in `rows`. Keep the
            # ones not yet attached pending until a message needs them — an
            # image is often sent just before the @ that refers to it.
            imgs = detect_images(rows, "/tmp/wxbridge_cap.png", W, _last_H, conv_left)
            if imgs:
                def inside_image(r):
                    for im_ in imgs:
                        x0, y0, x1, y1 = im_["box"]
                        if x0 - 6 <= r["x"] <= x1 and y0 - 6 <= r["y"] <= y1 + 6:
                            return True
                    return False
                kept = [r for r in rows if not inside_image(r)]
                if len(kept) != len(rows):
                    print(f"[bridge] 丢弃 {len(rows) - len(kept)} 行图片内部文字", flush=True)
                    rows = kept
                    # re-detect: with the image's own words gone, a second pass can
                    # reveal pictures the first one missed
                    imgs = detect_images(rows, "/tmp/wxbridge_cap.png", W, _last_H, conv_left)
            pending = [i for i in imgs
                       if (i["w"] // 10 * 10, i["h"] // 10 * 10) not in seen_images]
            paths = []
            for n, im_ in enumerate(pending):
                box = expand_box(im_["box"], W, _last_H, conv_left)
                out = Path(f"/tmp/wx_image_{n}.png")
                try:
                    from PIL import Image as _Img
                    _Img.open("/tmp/wxbridge_cap.png").crop(box).save(out)
                    paths.append(str(out))
                    seen_images.add((im_["w"] // 10 * 10, im_["h"] // 10 * 10))
                except Exception as e:
                    print(f"[bridge] 裁图失败: {e}", flush=True)

            text = build_prompt(targets, context, paths)
            try:
                resp = h.prompt(session_id, text)
            except Exception as e:
                log_event({"stage": "inject-failed", "err": f"{type(e).__name__}: {e}"})
                continue
            ok = (resp.get("result") or {}).get("ok")
            log_event({"stage": "injected", "mentioned_by": targets[-1][:50],
                       "ok": ok, "read_ms": t_read - t0,
                       "inject_ms": now_ms() - t_read})
    finally:
        det.terminate()


if __name__ == "__main__":
    sys.exit(main())
