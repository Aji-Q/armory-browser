#!/usr/bin/env python3
"""Create original vector-style Armory icons/promo (Pillow, build-time only)."""
from pathlib import Path
from PIL import Image, ImageDraw
ROOT=Path(__file__).resolve().parents[1]
TEAL=(26,89,77,255); CREAM=(248,246,239,255); AMBER=(224,177,88,255)

def icon(size):
    # Draw at native high-resolution coordinates; this is original artwork.
    scale=4; image=Image.new('RGBA',(size*scale,size*scale))
    draw=ImageDraw.Draw(image)
    def box(xy):return tuple(round(n*size*scale/128) for n in xy)
    draw.rounded_rectangle(box((16,16,112,112)),radius=round(size*scale*20/128),fill=TEAL)
    draw.line([box((39,89)),box((63,38)),box((87,89))],fill=CREAM,width=max(1,round(size*scale*9/128)),joint='curve')
    draw.line([box((49,70)),box((77,70))],fill=CREAM,width=max(1,round(size*scale*8/128)))
    draw.ellipse(box((85,27,101,43)),fill=AMBER)
    return image.resize((size,size),Image.Resampling.LANCZOS)

def main():
    icons=ROOT/'extension/icons';icons.mkdir(parents=True,exist_ok=True)
    store=ROOT/'store';store.mkdir(exist_ok=True)
    for size in (16,32,48,128):
        p=icons/f'icon{size}.png';icon(size).save(p)
        with Image.open(p) as x:assert x.size==(size,size) and x.format=='PNG'
    image=Image.new('RGB',(440,280),TEAL[:3]);draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((20,20,420,260),radius=24,outline=(63,121,105),width=1)
    draw.line((86,140,354,140),fill=(166,194,180),width=3)
    for x,color in [(86,CREAM),(220,AMBER),(354,CREAM)]:
        draw.ellipse((x-20,120,x+20,160),fill=color[:3])
    for x in (135,269):draw.polygon([(x,134),(x+9,140),(x,146)],fill=CREAM[:3])
    draw.rounded_rectangle((57,53,115,99),radius=8,outline=CREAM[:3],width=3)
    draw.line((67,65,105,65),fill=CREAM[:3],width=3)
    draw.line((67,77,94,77),fill=CREAM[:3],width=3)
    draw.ellipse((207,57,233,83),outline=AMBER[:3],width=3)
    draw.arc((197,82,243,118),180,360,fill=AMBER[:3],width=3)
    draw.rounded_rectangle((328,53,380,101),radius=8,outline=CREAM[:3],width=3)
    draw.line((339,78,350,89,369,66),fill=CREAM[:3],width=3)
    draw.line((354,175,354,206,86,206,86,175),fill=(166,194,180),width=2)
    draw.polygon([(80,181),(86,172),(92,181)],fill=CREAM[:3])
    image.save(store/'promo-440x280.png')
    with Image.open(store/'promo-440x280.png') as x:assert x.size==(440,280)
    print('Created and reopened icons16/32/48/128 PNG and original promo440x280 PNG')
if __name__=='__main__':main()
