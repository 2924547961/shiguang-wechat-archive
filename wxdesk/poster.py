"""Portable annual report image, generated without a browser or network."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def annual_poster(data, destination):
    canvas = Image.new('RGB', (1200, 1960), '#f6f5ef')
    draw = ImageDraw.Draw(canvas)
    font_path = next((p for p in [Path('C:/Windows/Fonts/msyh.ttc'), Path('C:/Windows/Fonts/simhei.ttf')] if p.exists()), None)
    def text(x,y,value,size=28,color='#283c32'):
        font = ImageFont.truetype(str(font_path),size) if font_path else ImageFont.load_default(size=size)
        draw.text((x,y),str(value),font=font,fill=color)
    text(80,65,'拾光 / 年度回顾',27,'#67806b')
    text(75,120,data['year'],115)
    text(80,265,data['title'][:28],34)
    draw.line((80,340,1120,340),fill='#d4ddcf',width=2)
    metrics=[('聊天消息',data['total'],'条'),('文字总量',data.get('characters',0),'字'),('活跃日期',data['days'],'天'),('最长连续聊天',data.get('longest_streak',0),'天')]
    for i,(label,value,unit) in enumerate(metrics):
        x,y=80+(i%2)*550,390+(i//2)*175
        text(x,y,label,26,'#67806b');text(x,y+45,f'{value:,}',58);text(x+360,y+70,unit,25)
    text(80,760,f"发送 {data['sent']:,} 条 / 收到 {data['received']:,} 条",29)
    text(80,810,f"发送文字 {data.get('sent_characters',0):,} 字 / 收到文字 {data.get('received_characters',0):,} 字",27)
    text(80,885,'十二个月的对话',34)
    maximum=max(data['months']+[1])
    for i,count in enumerate(data['months']):
        x=85+i*87; height=max(3,int(230*count/maximum))
        draw.rounded_rectangle((x,1220-height,x+53,1220),radius=7,fill='#789674')
        text(x+10,1235,str(i+1),22)
    text(80,1330,'日常里的细节',34)
    text(80,1400,f"活跃日平均 {data.get('daily_average',0):g} 条消息",28)
    text(80,1450,f"凌晨 00:00—06:00，共 {data.get('night_messages',0):,} 条消息",28)
    words=' / '.join(w['word'] for w in data.get('words',[])[:10])
    text(80,1540,'常用词',26,'#67806b')
    text(80,1590,words[:32] or '暂无足够文本',28)
    if len(words)>32:text(80,1635,words[32:64],28)
    draw.line((80,1760,1120,1760),fill='#d4ddcf',width=2)
    text(80,1800,'字数按文本与引用消息的非空白字符统计，含标点。',23,'#67806b')
    text(80,1840,'仅统计本地归档；完整日历、排行和消息类型见 HTML 年报。',23,'#67806b')
    canvas.save(destination,'PNG')
