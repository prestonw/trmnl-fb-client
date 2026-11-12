#!/usr/bin/env python3
"""trmnl-fb-client: poll a TRMNL (go-trmnl) server and render to an SPI LCD framebuffer.

Designed for the Waveshare ILI9486 on Raspberry Pi (fb, RGB565). Pulls the
current screen image from /api/display, downscales to the framebuffer size
(aspect-fill), converts to RGB565, and blits it to the framebuffer.

Config via env (see systemd unit) or defaults below.
"""
import os
import sys
import time
import json
import mmap
import urllib.request
import urllib.error
import struct

# --- config (env-overridable) ---
BASE_URL = os.environ.get("TRMNL_BASE_URL", "http://192.168.0.103:4567")
DEVICE_ID = os.environ.get("TRMNL_DEVICE_ID", "E4:5F:01:24:D5:87")   # MAC, = "ID" header
API_KEY   = os.environ.get("TRMNL_API_KEY", "pk92xYGUqjeKiPYdsul3zA")  # = "Access-Token" header
FB_DEVICE = os.environ.get("TRMNL_FB", "/dev/fb1")
FB_WIDTH  = int(os.environ.get("TRMNL_FB_WIDTH", "480"))
FB_HEIGHT = int(os.environ.get("TRMNL_FB_HEIGHT", "320"))
FB_STRIDE = int(os.environ.get("TRMNL_FB_STRIDE", "960"))   # bytes per row (width*2 for 16bpp)
FB_BPP    = int(os.environ.get("TRMNL_FB_BPP", "16"))
POLL_JITTER = int(os.environ.get("TRMNL_POLL_JITTER", "5"))
INVERT = os.environ.get("TRMNL_INVERT", "0") == "1"
ROTATE = int(os.environ.get("TRMNL_ROTATE", "0"))  # 0, 90 (CCW), 180, 270

from PIL import Image

def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)

def http_get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def http_get_bytes(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def display_headers():
    return {
        "ID": DEVICE_ID,
        "Access-Token": API_KEY,
        "Battery-Voltage": "4.2",
        "Model": "byod",
        "RSSI": "-25",
        "Width": str(FB_WIDTH),
        "Height": str(FB_HEIGHT),
        "Content-Type": "application/json",
    }

def aspect_fill(img, tw, th):
    """Scale img to cover (tw,th) and center-crop."""
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    x0 = (nw - tw) // 2
    y0 = (nh - th) // 2
    return img.crop((x0, y0, x0 + tw, y0 + th))

def to_rgb565_bytes(img, w, h):
    """img is RGB; return w*h*2 bytes in RGB565 big-endian (matches ILI9486 fb)."""
    rgb = img.convert("RGB")
    px = list(rgb.getdata())
    out = bytearray(w * h * 2)
    for i, (r, g, b) in enumerate(px):
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        out[i * 2 + 1] = (v >> 8) & 0xFF
        out[i * 2]     = v & 0xFF
    return bytes(out)

def blit(fb_fd, data):
    vmem = FB_WIDTH * FB_HEIGHT * (FB_BPP // 8)
    mm = mmap.mmap(fb_fd, vmem)
    # build rows padded to FB_STRIDE
    row_bytes = FB_WIDTH * (FB_BPP // 8)
    padded = bytearray(FB_HEIGHT * FB_STRIDE)
    for y in range(FB_HEIGHT):
        padded[y * FB_STRIDE:(y + 1) * FB_STRIDE] = data[y * row_bytes:(y + 1) * row_bytes]
    mm[0:len(padded)] = padded
    mm.flush()
    mm.close()

def render_frame():
    """One poll+render cycle. Returns the server refresh_rate in seconds."""
    d = http_get_json(BASE_URL + "/api/display", display_headers())
    status = d.get("status", -1)
    if status != 0:
        log(f"display status={status} (202=not ready) msg={d.get('message','')}")
        # 202 means not registered yet -> poll faster
        return d.get("refresh_rate") or 10
    url = d.get("image_url")
    if not url:
        log("no image_url in response", d)
        return d.get("refresh_rate") or 60
    raw = http_get_bytes(url)
    img = Image.open(__import__("io").BytesIO(raw))
    log(f"got image {img.size} mode={img.mode} <- {url.split('/')[-1]}")
    if INVERT:
        img = Image.eval(img, lambda p: 255 - p) if img.mode in ("L", "1") else img
    if ROTATE:
        img = img.rotate(ROTATE, expand=True)  # PIL rotates counter-clockwise
    img = aspect_fill(img, FB_WIDTH, FB_HEIGHT)
    data = to_rgb565_bytes(img, FB_WIDTH, FB_HEIGHT)
    fd = os.open(FB_DEVICE, os.O_RDWR)
    try:
        blit(fd, data)
    finally:
        os.close(fd)
    log(f"rendered {FB_WIDTH}x{FB_HEIGHT} to {FB_DEVICE}")
    return d.get("refresh_rate") or 60

def main():
    log(f"trmnl-fb-client start: {BASE_URL} id={DEVICE_ID} fb={FB_DEVICE} {FB_WIDTH}x{FB_HEIGHT}/{FB_BPP}")
    consecutive_errors = 0
    while True:
        try:
            wait = render_frame()
            consecutive_errors = 0
            if not wait or wait < 5:
                wait = 15
        except urllib.error.HTTPError as e:
            consecutive_errors += 1
            body = ""
            try: body = e.read().decode()[:200]
            except Exception: pass
            wait = 30 if e.code in (401, 404) else min(120, 10 * consecutive_errors)
            log(f"HTTP {e.code} (bad key/MAC if 401/404): {body}")
        except Exception as e:
            consecutive_errors += 1
            wait = min(120, 10 * consecutive_errors)
            log(f"error: {type(e).__name__}: {e}")
        time.sleep(wait + (time.time() % POLL_JITTER))

if __name__ == "__main__":
    main()
