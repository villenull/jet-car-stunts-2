from pathlib import Path
import json
import shutil
import zipfile
from PIL import Image, ImageOps, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'originals'
OUT = ROOT / 'library'
OUT.mkdir(exist_ok=True)
BG = '#17212e'

def fit(name, size, output, background=BG):
    im = Image.open(SRC / name).convert('RGBA')
    im = ImageOps.contain(im, size, Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', size, background)
    canvas.alpha_composite(im, ((size[0]-im.width)//2, (size[1]-im.height)//2))
    canvas.save(OUT / output)

# Only proportional scaling, cropping, and canvas padding. No artwork additions.
fit('igdb-cover.jpg', (600, 900), 'library-cover.png', '#e5e5e5')
fit('jcs2logo.png', (920, 430), 'library-header.png')
fit('jcs2logo.png', (1280, 425), 'library-logo.png', (0, 0, 0, 0))
hero = Image.open(SRC / 'app-screenshot-3.jpg').crop((0, 340, 1920, 960))
hero.save(OUT / 'library-hero.png')
fit('app-icon-512.jpg', (184, 184), 'app-icon.png')
shutil.copy2(SRC / 'app-icon-512.jpg', OUT / 'shortcut-icon.jpg')
Image.open(SRC / 'app-icon-512.jpg').save(OUT / 'shortcut-icon.ico', sizes=[(16,16),(32,32),(48,48),(128,128),(256,256)])
fit('jcs2titlebig.jpg', (920, 430), 'library-header-alternative.png')

board = Image.new('RGB', (1240, 1090), '#0d1420')
d = ImageDraw.Draw(board)
font = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
if not Path(font).exists():
    font = '/usr/share/fonts/liberation/LiberationSans-Regular.ttf'
def text(x,y,s,size=20,color='#edf2fa'):
    d.text((x,y),s,font=ImageFont.truetype(font,size),fill=color)
def tile(name, box):
    im=Image.open(OUT/name).convert('RGBA')
    im=ImageOps.contain(im, (box[2],box[3]), Image.Resampling.LANCZOS)
    d.rectangle((box[0],box[1],box[0]+box[2],box[1]+box[3]),fill=BG)
    board.paste(im,(box[0]+(box[2]-im.width)//2,box[1]+(box[3]-im.height)//2),im)

text(32,25,'JET CAR STUNTS 2 / LIBRARY ART PROPOSAL',28)
text(32,67,'Found online artwork. Proportional resizing, cropping or padding only.',18,'#aebdd0')
text(32,110,'01  COVER',20)
tile('library-cover.png',(32,148,300,450))
text(32,610,'600 x 900 / full cover retained',16,'#aebdd0')
text(364,110,'02  HERO',20)
tile('library-hero.png',(364,148,844,273))
text(364,433,'1920 x 620 / crop of an official screenshot',16,'#aebdd0')
text(364,477,'03  TRANSPARENT LOGO',20)
tile('library-logo.png',(364,512,610,203))
text(364,727,'1280 x 425 / original transparency retained',16,'#aebdd0')
text(1010,477,'04  ICON',20)
tile('shortcut-icon.jpg',(1010,518,184,184))
text(1010,718,'512 / 184 px',16,'#aebdd0')
text(32,786,'05  HORIZONTAL HEADER',20)
tile('library-header.png',(32,824,460,215))
text(518,786,'ALTERNATE HEADER / OFFICIAL BANNER',20)
tile('library-header-alternative.png',(518,824,460,215))
text(32,1052,'920 x 430 / padded to preserve the source image',16,'#aebdd0')
board.save(ROOT/'library-proposal.jpg',quality=95)

app=json.loads((SRC/'app-sources.json').read_text())
sources={
 'cover': {'url':'https://images.igdb.com/igdb/image/upload/t_1080p/co8bem.jpg','page':'https://gamepasscompare.com/app/61793/jet-car-stunts-2','transform':'Proportional fit to 600x900 with light gray padding; no cropping of the title.'},
 'hero': {'url':app['app-screenshot-3.jpg'],'page':'https://apps.apple.com/gb/app/jet-car-stunts-2/id708142626','transform':'Crop (0,340,1920,960) from 1920x1080 to 1920x620. No resizing.'},
 'logo_and_header': {'url':'https://trueaxis.com/jcs2logo.png','page':'https://trueaxis.com/jetcarstunts2.html','transform':'Proportional resizing; header adds a solid dark canvas. Logo retains alpha.'},
 'icons': {'url':app['app-icon-512.jpg'],'page':'https://apps.apple.com/gb/app/jet-car-stunts-2/id708142626','transform':'512px original retained; smaller icon downscaled proportionally.'},
 'alternate_header': {'url':'https://trueaxis.com/jcs2titlebig.jpg','page':'https://trueaxis.com/jetcarstunts2.html','transform':'Proportional fit with solid dark padding.'}
}
(ROOT/'sources.json').write_text(json.dumps(sources,indent=2)+'\n')
readme='''# Jet Car Stunts 2 — personal Steam library proposal

Preview: library-proposal.jpg. Ready-to-use candidates: library/.

| Slot | File | Size | Proposal |
|---|---|---|---|
| Portrait cover | library-cover.png | 600×900 | Found IGDB cover, scaled proportionally and padded to retain all lettering. |
| Hero | library-hero.png | 1920×620 | Official App Store screenshot, cropped to the wide library ratio. No text or HUD. |
| Logo | library-logo.png | 1280×425 | Original developer logo, transparent, proportionally enlarged. |
| Horizontal header | library-header.png | 920×430 | Original logo fitted onto a dark canvas. Clear at small sizes. |
| Alternate header | library-header-alternative.png | 920×430 | Developer's existing branded banner, scaled and padded without cropping. |
| Shortcut icon | shortcut-icon.jpg / shortcut-icon.ico | 512px JPG / multi-size ICO | Original App Store icon. |
| App icon | app-icon.png | 184×184 | Same icon, proportionally reduced. |

The hero uses the standard 1920×620 personal-library format to avoid needless enlargement. These are personal non-Steam library candidates, not a Steam store submission pack. The source logo is 600×199, so the larger PNG cannot gain new detail. The existing cover is somewhat soft. No generative edits, retouching, recoloring, added lettering, or stretching were used. The preview sheet labels are outside the asset files.

All source URLs and exact changes are in sources.json. Downloaded originals are retained in originals/. No images have been installed into Steam.
'''
(ROOT/'README.md').write_text(readme)
with zipfile.ZipFile(ROOT/'jcs2-library-proposal.zip','w',zipfile.ZIP_DEFLATED) as z:
    for f in sorted(OUT.iterdir()): z.write(f, 'library/'+f.name)
    for n in ['README.md','sources.json','library-proposal.jpg']:z.write(ROOT/n,n)
for f in sorted(OUT.iterdir()):
    im=Image.open(f)
    print(f.name, im.size)
