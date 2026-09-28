"""Create the Windows application icon from the existing 拾光 visual identity."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
size = 512
im = Image.new('RGBA', (size, size), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((12, 12, size - 12, size - 12), radius=110, fill='#194C3A')
d.ellipse((373, 70, 438, 135), fill='#D9BA74')
font = ImageFont.truetype(r'C:\Windows\Fonts\simhei.ttf', 294)
box = d.textbbox((0, 0), '拾', font=font)
x = (size - (box[2] - box[0])) / 2 - box[0] - 3
y = (size - (box[3] - box[1])) / 2 - box[1] + 19
d.text((x, y), '拾', font=font, fill='#FBF7EB', stroke_width=1)
target = root / 'wxdesk' / 'static' / 'app.ico'
im.save(target, format='ICO', sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                     (64, 64), (128, 128), (256, 256)])
(root / '.shiguang' / 'icon_preview.png').parent.mkdir(parents=True, exist_ok=True)
im.save(root / '.shiguang' / 'icon_preview.png')
print(target)
