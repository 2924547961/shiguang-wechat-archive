"""Loss-aware WeChat message parsing. No network lookups or XML entity expansion."""
from __future__ import annotations

import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import zstandard

KIND_NAMES = {"text": "文本", "image": "图片", "video": "视频", "audio": "语音", "emoji": "表情包",
              "file": "文件", "link": "分享链接", "system": "系统消息", "quote": "引用消息",
              "forward": "聊天记录", "transfer": "转账", "redpacket": "红包", "call": "音视频通话",
              "location": "位置", "contact": "名片", "miniapp": "小程序", "channel": "视频号", "unknown": "其他消息"}


def text_content(value):
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        if data.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                data = zstandard.ZstdDecompressor().decompress(data, max_output_size=32 * 1024 * 1024)
            except zstandard.ZstdError:
                return "[消息压缩内容无法解析]"
        value = data.decode("utf-8", "replace")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]", "", str(value))


def xml_root(text):
    if not text or len(text) > 32 * 1024 * 1024 or re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        return None
    start = text.find("<")
    if start < 0:
        return None
    try:
        return ET.fromstring(text[start:].strip())
    except (ET.ParseError, ValueError):
        # Some clients write unescaped URL query separators in XML attributes.
        # Repair only bare ampersands outside CDATA; keep valid entities intact.
        parts = re.split(r"(<!\[CDATA\[.*?\]\]>)", text[start:].strip(), flags=re.S)
        repaired = "".join(part if part.startswith("<![CDATA[") else
                           re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", part)
                           for part in parts)
        try:
            return ET.fromstring(repaired)
        except (ET.ParseError, ValueError):
            return None


def value(root, path, default=""):
    if root is None:
        return default
    n = root.find(path)
    return ("".join(n.itertext()).strip() if n is not None else default)


def safe_url(url):
    try:
        url = str(url).strip()
        p = urlsplit(url)
        return url if p.scheme.casefold() in {"http", "https"} and p.hostname and not p.username else ""
    except ValueError:
        return ""


def protobuf_fields(data: bytes):
    """Read only wire types needed by local packed_info_data; unknown fields are skipped."""
    if not data or len(data) > 4 * 1024 * 1024:
        return {}
    result, i = {}, 0
    def varint():
        nonlocal i
        n = shift = 0
        while i < len(data) and shift < 70:
            b = data[i]; i += 1; n |= (b & 127) << shift
            if b < 128:
                return n
            shift += 7
        raise ValueError("invalid varint")
    try:
        while i < len(data):
            tag = varint(); field, wire = tag >> 3, tag & 7
            if not field:
                break
            if wire == 0:
                item = varint()
            elif wire == 2:
                length = varint()
                if length > len(data) - i:
                    break
                item = data[i:i + length]; i += length
            elif wire in {1, 5}:
                length = 8 if wire == 1 else 4
                item = data[i:i + length]; i += length
            else:
                break
            result[field] = item
    except (ValueError, IndexError):
        pass
    return result


def packed_text(data, *fields):
    for field in fields:
        if not isinstance(data, bytes):
            return ""
        data = protobuf_fields(data).get(field, b"")
    return text_content(data).strip().strip('"') if isinstance(data, bytes) else ""


def system_text(root, text):
    content = value(root, ".//content")
    template = value(root, ".//template")
    if template:
        links = {}
        for node in root.findall(".//link"):
            plain = value(node, "plain")
            names = "、".join(n.text or "" for n in node.findall(".//nickname"))
            links[node.get("name", "")] = plain or names
        content = re.sub(r"\$([\w]+)\$", lambda m: links.get(m[1]) or m[0], template)
    if not content:
        content = value(root, ".//replacemsg")
    if not content and root is not None:
        content = "".join(root.itertext()).strip()
    return content or re.sub(r"<[^>]*>", "", text).strip() or "系统消息"


def parse_forward(root, depth=0):
    if root is None or depth > 8:
        return []
    record = root.find(".//recordinfo")
    if record is None:
        record = xml_root(value(root, ".//recorditem"))
    if record is None:
        record = root
    result = []
    for index, item in enumerate(record.findall(".//datalist/dataitem")[:5000]):
        dtype = item.get("datatype", "1")
        kind = {"1": "text", "2": "image", "3": "audio", "4": "video", "5": "link",
                "6": "location", "8": "file", "17": "forward", "19": "miniapp"}.get(dtype, "unknown")
        body = value(item, "datadesc") or value(item, "datatitle")
        obj = {"kind": kind, "body": body, "sender_name": value(item, "sourcename") or value(item, ".//realchatname"),
               "time": value(item, "sourcetime"), "url": safe_url(value(item, ".//link")),
               "md5": value(item, "datamd5") or value(item, "md5"), "filename": value(item, "datatitle"), "index": index}
        if kind == "forward":
            obj["items"] = parse_forward(xml_root(value(item, "recordxml")), depth + 1)
        result.append(obj)
    return result


def parse_message(local_type, content, sender="", packed=b""):
    text = text_content(content)
    if sender and text.startswith(sender + ":"):
        text = text[len(sender) + 1:].lstrip("\n")
    else:
        text = re.sub(r"^[\w@-]{1,96}:\n", "", text, count=1)
    low = int(local_type or 0) & 0xFFFFFFFF
    sub = int(local_type or 0) >> 32
    root = xml_root(text) if low != 1 else None
    kind = {1: "text", 3: "image", 34: "audio", 42: "contact", 43: "video", 47: "emoji",
            48: "location", 49: "link", 50: "call", 51: "call", 10000: "system", 10002: "system"}.get(low, "unknown")
    d = {}
    body = text if kind in {"text", "unknown"} else ""
    if kind == "system":
        body = system_text(root, text)
        if root is not None and (root.get('type') == 'revokemsg' or root.find('.//revokemsg') is not None):
            d = {'revoke': True,
                 'revoked_server_id': value(root, './/revokemsg/newmsgid') or value(root, './/revokemsg/msgid')}
    elif kind in {"image", "video", "emoji", "audio"}:
        tag = {"image": "img", "video": "videomsg", "emoji": "emoji", "audio": "voicemsg"}[kind]
        node = root.find(".//" + tag) if root is not None else None
        if node is None and root is not None and root.tag == tag:
            node = root
        attrs = dict(node.attrib) if node is not None else {}
        d = {"md5": attrs.get("md5", ""), "rawmd5": attrs.get("rawmd5", ""),
             "duration": attrs.get("voicelength", attrs.get("playlength", "")),
             "width": attrs.get("width", ""), "height": attrs.get("height", "")}
        if kind == "emoji":
            d["md5"] = attrs.get("androidmd5") or d["md5"]
            d["rawmd5"] = attrs.get("md5", "")
        if kind == "image":
            d["filename"] = packed_text(packed, 3, 4)
        if kind == "video":
            d["filename"] = packed_text(packed, 4, 8)
        if kind == "audio":
            d["transcript"] = packed_text(packed, 5, 2) or value(root, ".//voicetrans")
        body = d.get("transcript") or KIND_NAMES[kind]
    elif kind == "link":
        subtext = value(root, ".//appmsg/type")
        try:
            sub = int(subtext) if subtext else sub
        except ValueError:
            pass
        kind = {6: "file", 19: "forward", 57: "quote", 2000: "transfer", 2001: "redpacket",
                33: "miniapp", 36: "miniapp", 44: "miniapp", 51: "channel", 63: "channel", 88: "channel"}.get(sub, "link")
        d = {"title": value(root, ".//appmsg/title"), "description": value(root, ".//appmsg/des"),
             "url": safe_url(value(root, ".//appmsg/url")), "appname": value(root, ".//appinfo/appname") or value(root, ".//sourcedisplayname"),
             "md5": value(root, ".//appattach/filemd5") or value(root, ".//appmsg/md5")}
        body = d["title"] or d["description"] or KIND_NAMES[kind]
        if kind == "file":
            d.update(filename=packed_text(packed, 7, 1, 2) or d["title"], size=value(root, ".//totallen"), extension=value(root, ".//fileext"))
        elif kind == "quote":
            d["quote"] = {"server_id": value(root, ".//refermsg/svrid"), "sender": value(root, ".//refermsg/displayname"),
                          "body": value(root, ".//refermsg/content"), "type": value(root, ".//refermsg/type")}
            quote_root = xml_root(d["quote"]["body"])
            if quote_root is not None:
                d["quote"]["body"] = value(quote_root, ".//title") or "[引用的媒体消息]"
        elif kind == "forward":
            d["items"] = parse_forward(root)
            d["record_dir"] = packed_text(packed, 9, 1)
        elif kind == "transfer":
            d.update(amount=value(root, ".//feedesc"), memo=value(root, ".//pay_memo"), status=value(root, ".//paysubtype"))
            body = d["amount"] or body
        elif kind == "miniapp":
            d.update(appid=value(root, ".//weappinfo/appid"), page=value(root, ".//weappinfo/pagepath"))
        elif kind == "channel":
            d.update(author=value(root, ".//finderFeed/nickname"), description=value(root, ".//finderFeed/desc") or d["description"])
            body = d["description"] or body
    elif kind == "location":
        n = root.find(".//location") if root is not None else None
        if n is not None:
            d = {"latitude": n.get("x", ""), "longitude": n.get("y", ""), "label": n.get("label", ""), "name": n.get("poiname", "")}
            body = d["name"] or d["label"] or "位置分享"
    elif kind == "contact":
        n = root if root is not None else None
        if n is not None:
            d = {k: n.get(k, "") for k in ["username", "nickname", "alias", "province", "city", "sign"]}
            body = d["nickname"] or d["username"] or "联系人名片"
    elif kind == "call":
        body = value(root, ".//msg") or value(root, ".//displaycontent") or value(root, ".//content") or re.sub(r"<[^>]+>", "", text).strip() or "音视频通话"
    return {"kind": kind, "body": body, "detail": d, "raw": text}
