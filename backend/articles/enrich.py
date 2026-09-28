"""Best-effort extraction of albums, article media, and comments."""

from __future__ import annotations

from html import escape, unescape
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .downloader import Article, BASE, DownloadError, WeChatClient


def album_articles(client: WeChatClient, url: str, max_pages: int = 0) -> list[Article]:
    """Fetch album pages using the getalbum endpoint seen in the binary."""
    parsed = urlparse(url)
    if parsed.hostname != "mp.weixin.qq.com" or parsed.path != "/mp/appmsgalbum":
        raise DownloadError("请输入公众号合集链接")
    query = {k: v[-1] for k, v in parse_qs(parsed.query).items() if v}
    album_id = query.get("album_id")
    if not album_id:
        raise DownloadError("合集链接缺少 album_id")
    result: list[Article] = []
    seen: set[tuple[str, str]] = set()
    begin_msgid, begin_itemidx = "", "0"
    for page in range(max_pages or 1000):
        params = {"action": "getalbum", "album_id": album_id,
                  "begin_msgid": begin_msgid, "begin_itemidx": begin_itemidx, "f": "json"}
        response = client._get(BASE + "/mp/appmsgalbum", params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DownloadError("合集接口没有返回 JSON") from exc
        body = payload.get("getalbum_resp") or payload
        raw = body.get("article_list") or body.get("list") or []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        if isinstance(raw, dict):
            raw = raw.get("list") or []
        if not isinstance(raw, list):
            raise DownloadError("合集文章列表格式无法识别")
        new_count = 0
        for item in raw:
            msgid = str(item.get("msgid") or item.get("mid") or "")
            idx = str(item.get("itemidx") or item.get("idx") or "1")
            direct = item.get("url") or item.get("content_url") or ""
            if not direct and msgid:
                biz = query.get("__biz") or query.get("biz") or ""
                if biz:
                    direct = f"{BASE}/s?__biz={biz}&mid={msgid}&idx={idx}"
            if not direct:
                continue
            direct = urljoin(BASE, direct)
            marker = (msgid, idx) if msgid else (direct, "")
            if marker in seen:
                continue
            seen.add(marker)
            result.append(Article(item.get("title") or "文章", direct))
            new_count += 1
        if not raw or not new_count:
            break
        last = raw[-1]
        next_msgid = str(last.get("msgid") or last.get("mid") or "")
        next_idx = str(last.get("itemidx") or last.get("idx") or "1")
        if not next_msgid or (next_msgid, next_idx) == (begin_msgid, begin_itemidx):
            break
        begin_msgid, begin_itemidx = next_msgid, next_idx
        if body.get("is_over") in (1, "1", True):
            break
    return result


def _decoded_page(value: str) -> str:
    value = unescape(value).replace(r"\/", "/").replace(r"\u0026", "&").replace(r"\x26", "&")
    return value.replace(r"\u003d", "=").replace(r"\x3d", "=")


def collect_media(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Collect URLs embedded in WeChat article markup and page scripts."""
    result: dict[str, list[str]] = {"cover": [], "audio": [], "video": []}
    cover = soup.find("meta", attrs={"property": "og:image"})
    if cover and cover.get("content"):
        result["cover"].append(urljoin(BASE, cover["content"]))
    for node in soup.select("audio, video, source, iframe, mpvoice, mp-common-mpaudio"):
        src = (node.get("src") or node.get("data-src") or node.get("data-video-src") or
               node.get("data-mpvoice-src") or node.get("data-url"))
        if src:
            kind = "audio" if node.name in {"audio", "mpvoice", "mp-common-mpaudio"} or "audio" in (node.get("type") or "") else "video"
            result[kind].append(urljoin(BASE, _decoded_page(src)))
    page = _decoded_page(str(soup))
    for match in re.finditer(r'"voice_id"\s*:\s*"([\w\d]+)"', page):
        result["audio"].append(f"https://res.wx.qq.com/voice/getvoice?mediaid={match.group(1)}")
    for match in re.finditer(r'https?://(?:mpvideo\.qpic\.cn|finder\.video\.qq\.com)[^\s"\'<>\\]+', page):
        result["video"].append(match.group().rstrip("),;"))
    return {kind: list(dict.fromkeys(urls)) for kind, urls in result.items()}


def _resolve_video_page(client: WeChatClient, url: str) -> list[str]:
    parsed = urlparse(url)
    if parsed.hostname != "mp.weixin.qq.com" or not ("video" in parsed.path or "video" in parsed.query):
        return [url]
    response = client._get(url)
    page = _decoded_page(response.text)
    return list(dict.fromkeys(match.group().rstrip("),;") for match in re.finditer(
        r'https?://(?:mpvideo\.qpic\.cn|finder\.video\.qq\.com)[^\s"\'<>\\]+', page)))


def save_media(client: WeChatClient, soup: BeautifulSoup, folder: Path,
               enabled: set[str], log) -> list[Path]:
    targets: list[Path] = []
    for kind, urls in collect_media(soup).items():
        if kind not in enabled:
            continue
        resolved = []
        for url in urls:
            try:
                resolved.extend(_resolve_video_page(client, url) if kind == "video" else [url])
            except DownloadError as exc:
                log(f"媒体地址解析失败：{kind}：{exc}")
        for index, url in enumerate(dict.fromkeys(resolved), 1):
            ext = Path(urlparse(url).path).suffix.lower()
            allowed = {"cover": {".jpg", ".jpeg", ".png", ".webp"},
                       "audio": {".mp3", ".m4a", ".aac", ".amr"},
                       "video": {".mp4", ".mov"}}
            if ext not in allowed[kind]:
                ext = {"cover": ".jpg", "audio": ".mp3", "video": ".mp4"}[kind]
            target = folder / f"{kind}_{index:02d}{ext}"
            try:
                response = client._get(url, stream=True)
                size = 0
                with target.open("wb") as file:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            size += len(chunk)
                            if size > 500 * 1024 * 1024:
                                raise DownloadError("媒体文件超过 500 MB")
                            file.write(chunk)
                targets.append(target)
            except (DownloadError, OSError) as exc:
                target.unlink(missing_ok=True)
                log(f"媒体下载失败：{kind}：{exc}")
    return targets


def _page_variable(page: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}\s*=\s*[\'\"]([^\'\"]+)', page)
    return match.group(1) if match else ""


def fetch_comments(client: WeChatClient, soup: BeautifulSoup) -> list[dict]:
    """Request selected comments when the article exposes a comment id.

    WeChat changes these fields frequently; unsupported responses return [].
    """
    page = str(soup)
    comment_id = _page_variable(page, "comment_id")
    appmsgid = _page_variable(page, "appmsgid") or _page_variable(page, "mid")
    item_idx = _page_variable(page, "item_idx") or _page_variable(page, "idx") or "1"
    if not comment_id or not appmsgid:
        return []
    params = {"action": "getcomment", "comment_id": comment_id,
              "appmsgid": appmsgid, "idx": item_idx, "offset": 0, "limit": 100,
              "f": "json", **{k: v for k, v in client.auth.items() if k != "poc_sid"}}
    response = client._get(BASE + "/mp/appmsg_comment", params=params)
    try:
        payload = response.json()
    except ValueError:
        return []
    comments = payload.get("elected_comment") or payload.get("comment_list") or []
    return comments if isinstance(comments, list) else []


def comments_html(comments: list[dict]) -> str:
    if not comments:
        return ""
    lines = ["<section class=\"comments\"><h2>评论</h2>"]
    for item in comments:
        nickname = escape(str(item.get("nick_name") or item.get("nickname") or ""))
        content = escape(str(item.get("content") or ""))
        lines.append(f"<div class=\"comment\"><strong>{nickname}</strong><p>{content}</p></div>")
        for reply in item.get("reply_list") or []:
            rname = escape(str(reply.get("nick_name") or reply.get("nickname") or ""))
            rcontent = escape(str(reply.get("content") or ""))
            lines.append(f"<div class=\"reply\"><strong>{rname}</strong><p>{rcontent}</p></div>")
    lines.append("</section>")
    return "\n".join(lines)
