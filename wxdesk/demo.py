"""Explicitly labelled fictional data for repeatable UI and export verification."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import random
from pathlib import Path
from .common import atomic_json, identity
from .messages import parse_message
from .store import create_store, finalize, set_meta


def make_demo(state):
    state = Path(state); state.mkdir(parents=True, exist_ok=True)
    account = "wxid_demo_only_a1b2"; aid = identity(account)
    root = state / "accounts" / aid; root.mkdir(parents=True, exist_ok=True)
    assets = root / "assets"; assets.mkdir(exist_ok=True)
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (640, 400), "#e3ead9"); draw = ImageDraw.Draw(image)
    draw.ellipse((410, 44, 486, 120), fill="#eac49c")
    draw.polygon([(0,280),(130,152),(260,295),(405,118),(640,288),(640,400),(0,400)],fill="#99b09a")
    draw.polygon([(0,326),(165,252),(332,355),(500,230),(640,320),(640,400),(0,400)],fill="#617d68")
    draw.rectangle((0,342,640,400),fill="#b6c8ba"); draw.line((0,365,640,365),fill="#dce6d8",width=2)
    image.save(assets / "demo_landscape.png")
    (assets / "行程清单.txt").write_text("虚构演示附件\n周末去看山、散步、拍照。", "utf-8")
    atomic_json(state / "demo.json", {"active_count":1,"process_count":1,"access_denied":0,"accounts":[{
        "id":aid,"account":account,"username":"wxid_demo_only","display_name":"林间","root":"演示数据 · 非真实微信目录",
        "dbdir":"演示数据 · 非真实数据库","pids":[],"active":True,"has_archive":True,"legacy":None,"verified":True,"database_count":20}]})
    atomic_json(root / "account.json", {"account":account,"display_name":"林间","dbdir":"演示数据","root":"演示数据"})
    atomic_json(state / "settings.json", {"output_dir":str(state / "演示导出"),"include_media":True,"force_keys":False,"data_root":""})
    if (root / "archive.sqlite").exists():
        return
    c=create_store(root / "archive.sqlite"); rng=random.Random(20260926)
    people=[('demo_xiaoyu','小予','direct'),('demo_family@chatroom','一家人的小日子','group'),('demo_photos@chatroom','周末一起看世界','group'),
            ('demo_chen','陈一舟','direct'),('demo_mom','妈妈','direct'),('demo_book@chatroom','慢慢读书俱乐部','group'),
            ('gh_demo','一席','official'),('demo_fang','方糖','direct'),('demo_zhou','周小满','direct'),('demo_design@chatroom','设计的日常','group')]
    bodies=['今天的天气真好，出去走走吧。','周末一起去看山吧，听说那里的日落很美。','好呀，记得带上相机。','到家啦，一切顺利。','慢慢来，我们还有很多时间。','有空一起吃晚饭吧。','看到一片很好看的云，想分享给你。','谢谢你，今天很开心。','这本书读起来很温暖，推荐给你。','晚安，明天又是新的一天。','记得按时吃饭，照顾好自己。','照片已经整理好了，下次我们再去。']
    for cid,name,kind in people:
        c.execute("INSERT INTO contacts(username,nickname,remark,alias,kind) VALUES(?,?,?,?,?)",(cid,name,'',cid.replace('demo_',''),kind))
        c.execute("INSERT INTO conversations(id,title,kind) VALUES(?,?,?)",(cid,name,kind))
    c.execute("INSERT INTO contacts(username,nickname,remark,alias,kind) VALUES(?,?,?,?,?)",('wxid_demo_only','林间','','linjian','direct'))
    count=0
    def add(cid,sender,ts,ltype,content,detail=None,media='',status=''):
        nonlocal count
        count+=1; parsed=parse_message(ltype,content,sender)
        if detail is not None: parsed['detail']=detail
        name='我' if sender=='wxid_demo_only' else next((n for u,n,_ in people if u==sender),sender)
        c.execute("INSERT INTO messages(origin,conversation_id,sender,sender_name,is_self,ts,local_type,kind,body,detail,raw,source_db,source_table,local_id,server_id,media_path,media_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (str(count),cid,sender,name,int(sender=='wxid_demo_only'),ts,ltype,parsed['kind'],parsed['body'],json.dumps(parsed['detail'],ensure_ascii=False),parsed['raw'],'demo','demo',count,str(100000+count),media,status))
        return str(100000+count)
    for month in range(1,13):
        for n in range(150+month*17):
            cid,name,_=rng.choice(people);stamp=int(dt.datetime(2025,month,rng.randint(1,27),rng.choice([9,12,15,18,19,20,21,22]),rng.randint(0,59)).timestamp())
            add(cid,cid if n%2 else 'wxid_demo_only',stamp,1,rng.choice(bodies))
    base=int(dt.datetime(2026,9,25,16,20).timestamp());cid='demo_xiaoyu'
    original=add(cid,cid,base,1,'天气这么好，周末一起出去走走吧 ☀️')
    add(cid,'wxid_demo_only',base+60,1,'好呀！想去哪里？')
    add(cid,cid,base+120,1,'上次说的那片山野，听说现在很适合散步。')
    add(cid,cid,base+180,3,'<msg><img md5="demo"/></msg>',media='assets/demo_landscape.png',status='演示插画')
    add(cid,'wxid_demo_only',base+240,1,'好喜欢这片绿色。我们周六出发吧 🌿')
    add(cid,cid,base+300,49,f'<msg><appmsg><title>那就这么说定了！</title><type>57</type><refermsg><svrid>{original}</svrid><displayname>小予</displayname><content>天气这么好，周末一起出去走走吧 ☀️</content></refermsg></appmsg></msg>')
    add(cid,'wxid_demo_only',base+360,10000,'你拍了拍「小予」的肩膀')
    add(cid,cid,base+420,49,'<msg><appmsg><title>周末的小计划</title><type>19</type><recorditem><![CDATA[<recordinfo><datalist><dataitem datatype="1"><sourcename>小予</sourcename><datadesc>先去看日出，然后在湖边吃早餐。</datadesc></dataitem><dataitem datatype="1"><sourcename>林间</sourcename><datadesc>我来准备咖啡 ☕</datadesc></dataitem></datalist></recordinfo>]]></recorditem></appmsg></msg>')
    set_meta(c,'account',account);set_meta(c,'source','demo');set_meta(c,'limitations','所有人物、聊天和统计均为虚构演示数据。')
    finalize(c);c.close()
