#!/usr/bin/env python3
"""
Run: python3 generate_icons.py
Outputs: icon-192.png, icon-512.png  (place in android/app/src/main/res/mipmap-xxxhdpi/)
Requires: pip install Pillow
"""
from PIL import Image, ImageDraw, ImageFont
import math, os

def draw_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Dark green rounded background
    r = size // 5
    draw.rounded_rectangle([0, 0, size, size], radius=r,
                            fill=(6, 15, 9, 255))
    
    # Green arc (bridge arc) — top half circle
    cx, cy = size//2, size//2
    arc_r = int(size * 0.35)
    draw.arc(
        [cx-arc_r, cy-arc_r-int(size*0.08), cx+arc_r, cy+arc_r-int(size*0.08)],
        start=200, end=340,
        fill=(0, 200, 83, 255),
        width=max(3, size//40)
    )
    
    # Bridge deck (horizontal bar)
    bw = int(size * 0.6)
    bh = max(4, size // 35)
    by = cy + int(size * 0.05)
    draw.rectangle(
        [cx - bw//2, by, cx + bw//2, by + bh],
        fill=(0, 200, 83, 255)
    )
    
    # "AB" text
    fs = max(10, size // 5)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", fs)
    except:
        font = ImageFont.load_default()
    
    bbox = draw.textbbox((0,0), "AB", font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    tx = cx - tw//2
    ty = cy + int(size * 0.18)
    draw.text((tx, ty), "AB", fill=(0, 200, 83, 255), font=font)
    
    # Small wheat leaf
    leaf_x = cx - int(size * 0.08)
    leaf_y = cy - int(size * 0.38)
    leaf_r = int(size * 0.07)
    draw.ellipse(
        [leaf_x - leaf_r, leaf_y - leaf_r, leaf_x + leaf_r, leaf_y + leaf_r],
        fill=(0, 200, 83, 255)
    )
    
    return img

for sz in [192, 512]:
    icon = draw_icon(sz)
    icon.save(f"icon-{sz}.png", "PNG")
    print(f"Saved icon-{sz}.png")

print("Done! Copy both files to android/app/src/main/res/mipmap-xxxhdpi/")
print("Also copy icon-192.png → static/icon-192.png on your web project")
