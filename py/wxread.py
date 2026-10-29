#!/usr/bin/env python3
"""Step 1: WeChat read-only probe.

Capture the WeChat window, OCR it, segment into chat-list / conversation,
and diff against the previous frame to surface NEW messages.

Never clicks, never types. Read-only by construction.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
WINLIST = BIN / "winlist"
OCRJSON = BIN / "ocrjson"
CAPTURE = Path("/tmp/wx_cap.png")
STATE = HERE / "state.json"

CONF_MIN = 0.45

# Chrome that OCR picks up but that carries no conversation meaning.
UI_NOISE = {
    "发送", "<", ">", "••", "•••", "二", "⑨", "©", "∞~", "*<", "+）", "＋",
}


def find_window(keyword="微信"):
    out = subprocess.run([str(WINLIST)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f'owner="{keyword}"' not in line:
            continue
        # id=3813 owner="微信" title="微信" rect=942,138,644,640
        try:
            wid = int(line.split("id=")[1].split()[0])
            rect = line.split("rect=")[1]
            x, y, w, h = (int(v) for v in rect.split(","))
            return wid, (x, y, w, h)
        except Exception:
            continue
    return None, None


def conversation_left(items, width):
    """Locate the conversation panel's left edge.

    WeChat's chat list can be collapsed to a ~75pt icon rail or expanded to
    ~250pt, and the user can drag it — so a fixed ratio is wrong the moment
    the window is resized. Conversation content always includes at least one
    wide text run (a message, a header, a quote); the rail holds only short
    icon labels. The widest-run x is therefore a stable anchor.
    """
    wide = [i["x"] for i in items if i["w"] >= width * 0.08]
    if not wide:
        return int(width * 0.11)
    return max(int(width * 0.06), min(wide) - int(width * 0.02))


def capture(wid):
    if CAPTURE.exists():
        CAPTURE.unlink()
    r = subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), str(CAPTURE)],
                       capture_output=True)
    return r.returncode == 0 and CAPTURE.exists()


def ocr(path):
    r = subprocess.run([str(OCRJSON), str(path)], capture_output=True, text=True)
    if not r.stdout.strip():
        return []
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def group_rows(items):
    """Cluster OCR tokens into visual rows by vertical centre proximity.

    Tokens on the same text line share a baseline, so their centre-y values
    differ by only a few pixels; separate lines are further apart than the
    glyph height. Clustering on centre-y with a height-derived tolerance
    avoids the trap of growing a row's bounding box until it swallows the
    whole panel.
    """
    if not items:
        return []
    heights = sorted(i["h"] for i in items)
    tol = max(6, heights[len(heights) // 2] * 0.55)

    rows = []
    for it in sorted(items, key=lambda i: i["y"] + i["h"] / 2):
        cy = it["y"] + it["h"] / 2
        if rows and abs(cy - rows[-1]["cy"]) <= tol:
            row = rows[-1]
            row["items"].append(it)
            # weighted running centre
            total = row["cy"] * len(row["items"]) + cy
            row["cy"] = total / (len(row["items"]) + 1)
        else:
            rows.append({"cy": cy, "items": [it]})

    out = []
    for row in rows:
        row["items"].sort(key=lambda i: i["x"])
        # Two tokens sharing a baseline can still be unrelated elements: a
        # quoted line on the left and the "N new messages" divider on the
        # right sit on the same y. Split on a horizontal gap wider than a few
        # glyphs so each logical element becomes its own row.
        heights2 = sorted(i["h"] for i in row["items"])
        gap_tol = max(24, heights2[len(heights2) // 2] * 1.8)
        chunk = [row["items"][0]]
        chunks = []
        for it in row["items"][1:]:
            if it["x"] - (chunk[-1]["x"] + chunk[-1]["w"]) > gap_tol:
                chunks.append(chunk)
                chunk = [it]
            else:
                chunk.append(it)
        chunks.append(chunk)
        for ch in chunks:
            text = " ".join(i["t"] for i in ch).strip()
            if not text:
                continue
            out.append({
                "y": min(i["y"] for i in ch),
                "x": min(i["x"] for i in ch),
                "text": text,
                "conf": min(i["c"] for i in ch),
            })
    out.sort(key=lambda r: (r["y"], r["x"]))
    return out


def segment(items, width):
    conv_left = conversation_left(items, width)
    left = [i for i in items if i["x"] + i["w"] / 2 < conv_left]
    right = [i for i in items if i["x"] + i["w"] / 2 >= conv_left]
    return left, right, conv_left


def analyze():
    wid, rect = find_window()
    if wid is None:
        return {"error": "wechat window not found"}
    if not capture(wid):
        return {"error": "capture failed"}
    raw = ocr(CAPTURE)
    items = [i for i in raw if i["c"] >= CONF_MIN and i["t"].strip() not in UI_NOISE]
    from PIL import Image
    W, H = Image.open(CAPTURE).size
    left, right, conv_left = segment(items, W)
    conv = [r for r in group_rows(right) if r["text"]]
    return {
        "window": wid,
        "rect": rect,
        "px": [W, H],
        "conv_left": conv_left,
        "rail": [r["text"] for r in group_rows(left) if r["text"]],
        "conversation": [{"y": r["y"], "x": r["x"], "text": r["text"], "conf": round(r["conf"], 2)}
                         for r in conv],
    }


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            return {}
    return {}


def main():
    watch = "--watch" in sys.argv
    interval = 4.0
    for a in sys.argv:
        if a.startswith("--interval="):
            interval = float(a.split("=")[1])

    prev = load_state()
    prev_texts = set(prev.get("conversation_texts") or [])

    rounds = 5 if watch else 1
    for n in range(rounds):
        data = analyze()
        if "error" in data:
            print(f"[{n}] ERROR: {data['error']}")
            return 1
        conv = data["conversation"]
        texts = [c["text"] for c in conv]
        new = [t for t in texts if t not in prev_texts] if prev_texts else []
        print(f"\n=== round {n} | window {data['window']} rect={data['rect']} px={data['px']} conv_left={data['conv_left']} ===")
        print(f"rail ({len(data['rail'])}):")
        for t in data["rail"][:8]:
            print(f"   · {t}")
        print(f"conversation ({len(conv)}):")
        for c in conv[:14]:
            mark = " NEW" if c["text"] in new else ""
            print(f"   [{c['y']:>4}] conf={c['conf']:<4} {c['text']}{mark}")
        if new:
            print(f">>> NEW since last frame: {len(new)}")
            for t in new:
                print(f"    + {t}")
        elif prev_texts:
            print(">>> no new messages")
        STATE.write_text(json.dumps({"conversation_texts": texts}, ensure_ascii=False))
        prev_texts = set(texts)
        if watch and n < rounds - 1:
            time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
