"""Generate high-resolution branded assets for SmarterGamers Vertical Blur Editor.
Generates:
- assets/app_icon.png (512x512)
- assets/app_icon.ico (Windows multi-size ICO: 16, 24, 32, 48, 64, 128, 256)
- assets/banner.png (1280x420 README banner)
"""

import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)


def draw_rounded_rect(draw, bbox, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(bbox, radius=radius, fill=fill, outline=outline, width=width)


def create_app_icon():
    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. Subtle drop shadow behind the squircle
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow)
    s_draw.rounded_rectangle((24, 28, 488, 492), radius=100, fill=(0, 0, 0, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(16))
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)

    # 2. Main squircle background - Obsidian Slate
    # Gradient simulation from top-left #1a1d26 to bottom-right #0d0e12
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    b_draw = ImageDraw.Draw(base)
    b_draw.rounded_rectangle((28, 24, 484, 480), radius=96, fill=(18, 20, 26, 255), outline=(50, 56, 72, 255), width=3)

    # Metallic highlight top edge
    b_draw.line((80, 25, 432, 25), fill=(100, 115, 145, 180), width=2)
    img = Image.alpha_composite(img, base)
    draw = ImageDraw.Draw(img)

    # 3. Outer Neon Glow - SmarterGamers Cyan & Neon Green
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    g_draw = ImageDraw.Draw(glow)
    # 9:16 phone / vertical video frame in background (blurred)
    # Aspect 9:16 -> width 200, height 355
    v_left, v_top, v_right, v_bottom = 156, 78, 356, 426
    g_draw.rounded_rectangle((v_left - 12, v_top - 12, v_right + 12, v_bottom + 12), radius=32, fill=(0, 240, 255, 60))
    g_draw.rounded_rectangle((v_left - 6, v_top - 6, v_right + 6, v_bottom + 6), radius=28, fill=(143, 212, 0, 70))
    glow = glow.filter(ImageFilter.GaussianBlur(18))
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    # 4. Vertical 9:16 Container (The blurred background layer)
    v_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    v_draw = ImageDraw.Draw(v_layer)
    v_draw.rounded_rectangle((v_left, v_top, v_right, v_bottom), radius=24, fill=(28, 32, 44, 255), outline=(0, 240, 255, 160), width=2)
    
    # Simulate blurred video background pattern inside the vertical container
    for y in range(v_top + 10, v_bottom - 10, 8):
        alpha = int(40 + 30 * math.sin(y * 0.05))
        v_draw.line((v_left + 10, y, v_right - 10, y), fill=(80, 100, 150, alpha), width=4)
    v_layer = v_layer.filter(ImageFilter.GaussianBlur(4))
    img = Image.alpha_composite(img, v_layer)
    draw = ImageDraw.Draw(img)

    # 5. Crisp 16:9 Landscape Screen Overlay (The centered sharp video)
    # Width 270, height 152
    h_left, h_top, h_right, h_bottom = 121, 180, 391, 324
    
    # Sharp screen shadow
    s_screen = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ss_draw = ImageDraw.Draw(s_screen)
    ss_draw.rounded_rectangle((h_left - 4, h_top + 4, h_right + 4, h_bottom + 12), radius=16, fill=(0, 0, 0, 220))
    s_screen = s_screen.filter(ImageFilter.GaussianBlur(10))
    img = Image.alpha_composite(img, s_screen)
    draw = ImageDraw.Draw(img)

    # Sharp screen body
    screen = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sc_draw = ImageDraw.Draw(screen)
    sc_draw.rounded_rectangle((h_left, h_top, h_right, h_bottom), radius=14, fill=(12, 14, 18, 255), outline=(143, 212, 0, 255), width=3)
    
    # Screen inner gaming display gradient
    sc_draw.rounded_rectangle((h_left + 4, h_top + 4, h_right - 4, h_bottom - 4), radius=10, fill=(22, 28, 38, 255))
    
    # Gaming play / blur focus icon in center of the sharp screen
    center_x = (h_left + h_right) // 2
    center_y = (h_top + h_bottom) // 2
    
    # Play triangle in neon green / cyan
    tri_pts = [
        (center_x - 14, center_y - 20),
        (center_x + 18, center_y),
        (center_x - 14, center_y + 20),
    ]
    sc_draw.polygon(tri_pts, fill=(143, 212, 0, 255))

    # Fast forward / vertical pulse arrows
    sc_draw.line((v_left + 14, center_y, h_left - 8, center_y), fill=(0, 240, 255, 200), width=3)
    sc_draw.line((h_right + 8, center_y, v_right - 14, center_y), fill=(0, 240, 255, 200), width=3)

    img = Image.alpha_composite(img, screen)
    draw = ImageDraw.Draw(img)

    # 6. SmarterGamers "SG" Brand Monogram Badge (Bottom Right)
    badge_x, badge_y = 390, 390
    draw.ellipse((badge_x - 28, badge_y - 28, badge_x + 28, badge_y + 28), fill=(15, 17, 23, 255), outline=(143, 212, 0, 240), width=2)
    # Mini SG text or thunderbolt
    bolt_pts = [
        (badge_x + 2, badge_y - 16),
        (badge_x - 10, badge_y + 1),
        (badge_x - 2, badge_y + 1),
        (badge_x - 4, badge_y + 16),
        (badge_x + 10, badge_y - 1),
        (badge_x + 2, badge_y - 1),
    ]
    draw.polygon(bolt_pts, fill=(143, 212, 0, 255))

    # Save PNG
    png_path = ASSETS / "app_icon.png"
    img.save(png_path, format="PNG")
    print(f"Saved {png_path}")

    # Save Windows Multi-Size ICO
    ico_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    ico_path = ASSETS / "app_icon.ico"
    img.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"Saved {ico_path}")


def create_banner():
    w, h = 1280, 420
    img = Image.new("RGBA", (w, h), (13, 15, 20, 255))
    draw = ImageDraw.Draw(img)

    # Grid background pattern
    for x in range(0, w, 40):
        draw.line((x, 0, x, h), fill=(25, 29, 38, 120), width=1)
    for y in range(0, h, 40):
        draw.line((0, y, w, y), fill=(25, 29, 38, 120), width=1)

    # Cyber radiant glows
    glow1 = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    g1_draw = ImageDraw.Draw(glow1)
    g1_draw.ellipse((100, -100, 700, 500), fill=(0, 240, 255, 30))
    g1_draw.ellipse((600, 100, 1200, 600), fill=(143, 212, 0, 25))
    glow1 = glow1.filter(ImageFilter.GaussianBlur(90))
    img = Image.alpha_composite(img, glow1)
    draw = ImageDraw.Draw(img)

    # Add icon on the left
    icon = Image.open(ASSETS / "app_icon.png").resize((240, 240), Image.Resampling.LANCZOS)
    img.paste(icon, (70, 90), icon)

    # Text rendering
    font_large = None
    font_sub = None
    font_badge = None
    try:
        font_large = ImageFont.truetype("segoeui.ttf", 46)
        font_sub = ImageFont.truetype("segoeuil.ttf", 22)
        font_bold = ImageFont.truetype("segoeuib.ttf", 22)
        font_badge = ImageFont.truetype("segoeui.ttf", 15)
    except Exception:
        try:
            font_large = ImageFont.truetype("arial.ttf", 44)
            font_sub = ImageFont.truetype("arial.ttf", 20)
            font_bold = ImageFont.truetype("arialbd.ttf", 20)
            font_badge = ImageFont.truetype("arial.ttf", 14)
        except Exception:
            pass

    # Category Pill
    draw.rounded_rectangle((350, 85, 590, 115), radius=15, fill=(24, 30, 42, 255), outline=(0, 240, 255, 180), width=1)
    draw.text((365, 90), "SMARTERGAMERS STUDIO", fill=(0, 240, 255, 255), font=font_badge)

    # App Title
    draw.text((350, 130), "Vertical Blur Editor", fill=(255, 255, 255, 255), font=font_large)

    # Subtitle
    draw.text((350, 195), "Transform 16:9 Landscape Gameplay into 9:16 Shorts & Reels", fill=(170, 180, 200, 255), font=font_sub)
    draw.text((350, 230), "Ultra-Fast GPU Acceleration  •  NVENC / QSV / AMF  •  Batch Processing  •  Live Preview", fill=(143, 212, 0, 255), font=font_bold)

    # Badges / Pills
    pills = [
        ("⚡ 10x Fast BoxBlur Pipeline", (350, 280, 580, 315)),
        ("🎮 RTX / NVENC Hardware Accelerated", (600, 280, 910, 315)),
        ("📦 Windows Installer & Portable EXE", (930, 280, 1220, 315)),
    ]
    for text, bbox in pills:
        draw.rounded_rectangle(bbox, radius=8, fill=(20, 24, 32, 230), outline=(60, 70, 90, 255), width=1)
        draw.text((bbox[0] + 12, bbox[1] + 7), text, fill=(220, 230, 245, 255), font=font_badge)

    # Border frame
    draw.rectangle((0, 0, w - 1, h - 1), outline=(40, 45, 60, 255), width=1)

    banner_path = ASSETS / "banner.png"
    img.save(banner_path, format="PNG")
    print(f"Saved {banner_path}")


if __name__ == "__main__":
    create_app_icon()
    create_banner()
