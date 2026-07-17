"""genDISK 앱 아이콘을 PIL 로 그린다 — 안드로이드 적응형 아이콘과 동일한 디자인.

배경 #1565C0 둥근 사각형 + 흰색 'G' 모노그램(오른쪽이 열린 디스크 링 + 가로 막대).
트레이 아이콘·창 아이콘·exe 아이콘(.ico)에 같은 그림을 쓴다.
"""
import os
import sys

BG = (21, 101, 192, 255)     # #1565C0
FG = (255, 255, 255, 255)

# 안드로이드 ic_launcher_foreground.xml 의 108 viewport 기준 좌표.
_RING_C = (54.0, 54.0)
_RING_R = 28.0
_RING_STROKE = 14.0
_RING_ENDS = ((77.2, 69.65), (77.2, 38.35))   # 링 양 끝(둥근 캡)
_BAR = ((54.0, 54.0), (76.0, 54.0))
_BAR_STROKE = 12.0


def render_icon(size: int):
    """size×size RGBA 아이콘 Image 를 돌려준다 (4x 슈퍼샘플링 후 축소해 부드럽게)."""
    from PIL import Image, ImageDraw

    ss = 4
    s = size * ss
    sc = s / 108.0
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def pt(x, y):
        return (x * sc, y * sc)

    def dot(cx, cy, r):
        x, y = pt(cx, cy)
        rr = r * sc
        d.ellipse([x - rr, y - rr, x + rr, y + rr], fill=FG)

    # 배경: 둥근 사각형
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(20 * sc), fill=BG)

    # 링(오른쪽 열림): 3시 방향(0°)에서 시계방향. 아래끝 34° → 위끝 326° 까지 긴 호.
    cx, cy = _RING_C
    box = [pt(cx - _RING_R, cy - _RING_R), pt(cx + _RING_R, cy + _RING_R)]
    d.arc([box[0][0], box[0][1], box[1][0], box[1][1]],
          start=34, end=326, fill=FG, width=int(_RING_STROKE * sc))
    for ex, ey in _RING_ENDS:          # 링 끝 둥근 캡
        dot(ex, ey, _RING_STROKE / 2)

    # G 가로 막대
    (x0, y0), (x1, y1) = _BAR
    d.line([pt(x0, y0), pt(x1, y1)], fill=FG, width=int(_BAR_STROKE * sc))
    dot(x0, y0, _BAR_STROKE / 2)
    dot(x1, y1, _BAR_STROKE / 2)

    return img.resize((size, size), Image.LANCZOS)


def icon_path() -> str:
    """번들된 gendisk.ico 경로. onefile 은 _MEIPASS, 소스 실행은 win_x64/."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "gendisk.ico")
