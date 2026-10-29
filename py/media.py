#!/usr/bin/env python3
"""Media-region detection for the WeChat conversation panel.

Text OCR is cheap and exact but blind to pictures, videos and stickers. This
finds the large "busy" areas of the conversation — regions with heavy local
colour variation, which flat UI chrome never has — so only those crops need a
vision model. Most turns therefore cost zero vision calls.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
OCRJSON = BIN / "ocrjson"
WINLIST = BIN / "winlist"
CAP = Path("/tmp/wx_media.png")

BLOCK = 16          # pixel block for sampling
COLOR_THRESHOLD = 10  # per-block colourfulness (max-min RGB); flat UI sits at 0-2
DEV_THRESHOLD = 25    # per-block luminance deviation, catches greyscale media
MIN_AREA = 0.02       # fraction of panel area a media region must exceed
DILATE = 2            # blocks of morphological closing, so a photo's smooth
                      # interior and its textured edges form one region


def window():
    out = subprocess.run([str(WINLIST)], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if 'owner="微信"' not in line:
            continue
        wid = int(line.split("id=")[1].split()[0])
        return wid
    return None


def ocr(path):
    r = subprocess.run([str(OCRJSON), str(path)], capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "[]")
    except Exception:
        return []


def detect(path):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()

    # Panel bounds: right of the icon rail, above the compose separator.
    toks = [t for t in ocr(path) if t["c"] >= 0.3]
    wide = [t["x"] for t in toks if t["w"] >= W * 0.08]
    conv_left = max(int(W * 0.06), min(wide) - int(W * 0.02)) if wide else int(W * 0.12)
    panel_bottom = H
    for y in range(int(H * 0.65), H):
        bright = sum(1 for x in range(0, W, 4) if sum(px[x, y]) / 3 > 45)
        if bright > (W / 4) * 0.8:
            panel_bottom = y
            break

    # Per-block colourfulness + luminance deviation over the panel.
    gw = (W - conv_left) // BLOCK
    gh = (panel_bottom) // BLOCK
    busy = [[False] * gw for _ in range(gh)]
    for gy in range(gh):
        for gx in range(gw):
            x0 = conv_left + gx * BLOCK
            y0 = gy * BLOCK
            if y0 < 70:          # window header band
                continue
            vals = []
            col = []
            greenish = 0
            n = 0
            for yy in range(y0, min(y0 + BLOCK, panel_bottom), 3):
                for xx in range(x0, min(x0 + BLOCK, W), 3):
                    r, g, b = px[xx, yy]
                    vals.append((r + g + b) / 3)
                    col.append(max(r, g, b) - min(r, g, b))
                    # WeChat's own outgoing bubble is a saturated green; it is
                    # chrome, not media, and would otherwise read as "colourful".
                    if g > r + 30 and g > b + 30:
                        greenish += 1
                    n += 1
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            dev = sum(abs(v - mean) for v in vals) / len(vals)
            c = sum(col) / len(col)
            if greenish > n * 0.5:
                continue
            busy[gy][gx] = c > COLOR_THRESHOLD or dev > DEV_THRESHOLD

    # Morphological closing: grow then shrink, so textured edges and smooth
    # interiors join into a single region instead of many fragments.
    for _ in range(DILATE):
        grown = [row[:] for row in busy]
        for gy in range(gh):
            for gx in range(gw):
                if busy[gy][gx]:
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            ny, nx = gy + dy, gx + dx
                            if 0 <= ny < gh and 0 <= nx < gw:
                                grown[ny][nx] = True
        busy = grown

    # Connected components over busy cells.
    seen = [[False] * gw for _ in range(gh)]
    regions = []
    for gy in range(gh):
        for gx in range(gw):
            if not busy[gy][gx] or seen[gy][gx]:
                continue
            stack = [(gx, gy)]
            seen[gy][gx] = True
            cells = []
            while stack:
                cx, cy = stack.pop()
                cells.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < gw and 0 <= ny < gh and busy[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            xs = [c[0] for c in cells]
            ys = [c[1] for c in cells]
            x0 = conv_left + min(xs) * BLOCK
            x1 = conv_left + (max(xs) + 1) * BLOCK
            y0 = min(ys) * BLOCK
            y1 = (max(ys) + 1) * BLOCK
            area = (x1 - x0) * (y1 - y0)
            if area < (W - conv_left) * panel_bottom * MIN_AREA:
                continue
            regions.append({"box": [x0, y0, x1, y1], "area": area})

    regions.sort(key=lambda r: -r["area"])
    return {
        "image": [W, H],
        "conv_left": conv_left,
        "panel_bottom": panel_bottom,
        "regions": regions,
    }


def main():
    wid = window()
    if wid is None:
        print("no wechat window")
        return 1
    if CAP.exists():
        CAP.unlink()
    subprocess.run(["screencapture", "-x", "-o", "-l", str(wid), str(CAP)], capture_output=True)

    data = detect(CAP)
    print(f"image={data['image']} conv_left={data['conv_left']} panel_bottom={data['panel_bottom']}")
    print(f"media regions: {len(data['regions'])}")
    from PIL import Image
    im = Image.open(CAP)
    for i, r in enumerate(data["regions"][:5]):
        x0, y0, x1, y1 = r["box"]
        # pad a little so the vision model sees context
        pad = 8
        box = (max(0, x0 - pad), max(0, y0 - pad), min(im.width, x1 + pad), min(im.height, y1 + pad))
        out = Path(f"/tmp/wx_media_{i}.png")
        im.crop(box).save(out)
        print(f"  [{i}] box={list(box)} size={box[2]-box[0]}x{box[3]-box[1]} area_px={r['area']} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
