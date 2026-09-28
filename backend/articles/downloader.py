"""Independent reconstruction of the observed article-list/download workflow."""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import datetime, date
import json
from pathlib import Path
import re
import threading
from typing import Callable, Iterator
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup


BASE = "https://mp.weixin.qq.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
AUTH_KEYS = ("uin", "key", "pass_ticket", "poc_token", "poc_sid")


class DownloadError(Exception):
    pass


class Stopped(Exception):
    pass


@dataclass
class Article:
    title: str
    url: str
    published: datetime | None = None
    author: str = ""


@dataclass
class Options:
    output: Path
    fmt: str = "html"
    formats: tuple[str, ...] = ()
    images: bool = True
    keyword: str = ""
    start_date: date | None = None
    end_date: date | None = None
    delay: float = 1.5
    max_pages: int = 0
    cover: bool = False
    audio: bool = False
    video: bool = False
    comments: bool = False
    skip_existing: bool = True
    batch_size: int = 10
    retries: int = 1


SUPPORTED_FORMATS = ("html", "md", "txt", "mhtml", "docx", "pdf", "json")


def parse_article_links(value: str) -> list[str]:
    """One article URL per line; preserve order and remove duplicates."""
    result: list[str] = []
    seen: set[str] = set()
    for line_no, line in enumerate(value.splitlines(), 1):
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        parse_input_url(url)
        parsed = urlparse(url)
        if parsed.path not in ("/s",) and not parsed.path.startswith("/s/"):
            raise DownloadError(f"第 {line_no} 行不是公众号文章链接")
        if url not in seen:
            result.append(url)
            seen.add(url)
    if not result:
        raise DownloadError("请每行粘贴一条公众号文章链接")
    return result


def parse_input_url(value: str) -> tuple[str, dict[str, str]]:
    """Accept a WeChat article/profile URL with optional session parameters."""
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com":
        raise DownloadError("请输入 https://mp.weixin.qq.com 的文章或公众号链接")
    query = parse_qs(parsed.query, keep_blank_values=True)
    params = {key: values[-1] for key, values in query.items() if values}
    return params.get("__biz", ""), params


def safe_name(value: str, limit: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or "未命名")[:limit].rstrip(" .")


def article_from_message(message: dict) -> list[Article]:
    """Flatten a historical push, including its secondary articles."""
    try:
        timestamp = int(message.get("comm_msg_info", {}).get("datetime", 0))
        published = datetime.fromtimestamp(timestamp) if timestamp else None
    except (TypeError, ValueError, OverflowError):
        published = None
    info = message.get("app_msg_ext_info") or {}
    items = [info, *(info.get("multi_app_msg_item_list") or [])] if info else []
    result = []
    for item in items:
        url = item.get("content_url") or ""
        if url:
            result.append(Article(item.get("title") or "未命名", urljoin(BASE, url), published,
                                  item.get("author") or ""))
    return result


def parse_history_response(payload: dict) -> tuple[list[Article], int, bool]:
    """Normalize both JSON-object and JSON-string general_msg_list forms."""
    err = payload.get("base_resp") or {}
    ret = err.get("ret", payload.get("ret", 0))
    if ret not in (0, "0", None):
        raise DownloadError(f"历史消息接口返回错误 {ret}: {err.get('errmsg', '')}")
    raw = payload.get("general_msg_list") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DownloadError("历史消息列表不是有效 JSON") from exc
    if not isinstance(raw, dict):
        raise DownloadError("历史消息列表格式无法识别")
    messages = raw.get("list") or []
    articles = [a for message in messages for a in article_from_message(message)]
    try:
        next_offset = int(payload.get("next_offset", 0))
    except (TypeError, ValueError):
        next_offset = 0
    can_continue = bool(int(payload.get("can_msg_continue", 0) or 0))
    return articles, next_offset, can_continue


class WeChatClient:
    def __init__(self, auth: dict[str, str] | None = None, session: requests.Session | None = None):
        self.auth = {k: v for k, v in (auth or {}).items() if k in AUTH_KEYS and v}
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Referer": BASE + "/"})
        if self.auth.get("poc_sid"):
            self.session.cookies.set("poc_sid", self.auth["poc_sid"], domain="mp.weixin.qq.com")

    def _get(self, url: str, **kwargs) -> requests.Response:
        try:
            for _ in range(6):
                parsed = urlparse(url)
                host = (parsed.hostname or '').lower()
                if parsed.scheme != 'https' or not host or parsed.username or parsed.password or not (
                        host in {'qq.com', 'qpic.cn', 'weixin.qq.com'} or
                        host.endswith(('.qq.com', '.qpic.cn', '.weixin.qq.com'))):
                    raise DownloadError('文章资源地址不受支持。')
                response = self.session.get(url, timeout=(10, 30), allow_redirects=False, **kwargs)
                if response.is_redirect:
                    location = response.headers.get('Location')
                    response.close()
                    if not location:
                        raise DownloadError('文章资源跳转缺少目标地址。')
                    url = urljoin(url, location)
                    kwargs.pop('params', None)
                    continue
                response.raise_for_status()
                return response
            raise DownloadError('文章资源跳转次数过多。')
        except DownloadError:
            raise
        except requests.RequestException as exc:
            raise DownloadError(f"网络请求失败: {exc}") from exc

    def verify_session(self, biz: str) -> bool:
        """Probe the profile home endpoint; history may still be gated separately."""
        if not biz or not all(self.auth.get(k) for k in ("uin", "key", "pass_ticket")):
            return False
        params = {"action": "home", "__biz": biz, "scene": 124,
                  **{k: v for k, v in self.auth.items() if k != "poc_sid"}}
        response = self._get(BASE + "/mp/profile_ext", params=params)
        body = response.text.lower()
        return "__biz" in body and not any(token in body for token in ("access denied", "invalid key", "验证身份"))

    def resolve_biz(self, article_url: str) -> str:
        """Extract the account id from an ordinary short article URL."""
        biz, _ = parse_input_url(article_url)
        if biz:
            return biz
        response = self._get(article_url)
        if "/mp/wappoc_appmsgcaptcha" in response.url:
            raise DownloadError("微信返回访问验证页；请在微信内打开文章后再获取会话参数")
        patterns = (
            r'\b(?:var\s+)?biz\s*=\s*["\']([^"\']+)',
            r'\bwindow\.biz\s*=\s*["\']([^"\']+)',
            r'__biz=([A-Za-z0-9+/=]+)',
        )
        for pattern in patterns:
            match = re.search(pattern, response.text)
            if match:
                return match.group(1)
        raise DownloadError("文章页面中未找到公众号 __biz")

    def history(self, biz: str, stop: threading.Event, max_pages: int = 0,
                delay: float = 1.5) -> Iterator[Article]:
        if not biz:
            raise DownloadError("链接中没有 __biz，无法确定公众号")
        if not all(self.auth.get(k) for k in ("uin", "key", "pass_ticket")):
            raise DownloadError("批量模式需要 uin、key、pass_ticket")
        offset, page = 0, 0
        seen_offsets: set[int] = set()
        while not stop.is_set() and (max_pages <= 0 or page < max_pages):
            if offset in seen_offsets:
                raise DownloadError("分页偏移重复，已停止以避免循环")
            seen_offsets.add(offset)
            params = {
                "action": "getmsg", "__biz": biz, "f": "json", "offset": offset,
                "count": 10, "is_ok": 1, "scene": 124,
                **{k: v for k, v in self.auth.items() if k != "poc_sid"},
            }
            response = self._get(BASE + "/mp/profile_ext", params=params)
            try:
                payload = response.json()
            except ValueError as exc:
                raise DownloadError("历史消息接口未返回 JSON；会话可能失效或需要在微信内访问") from exc
            articles, next_offset, more = parse_history_response(payload)
            for article in articles:
                if stop.is_set():
                    raise Stopped()
                yield article
            page += 1
            if not more or not articles:
                break
            offset = next_offset
            if stop.wait(max(0, delay)):
                raise Stopped()

    def fetch_article(self, article: Article) -> tuple[BeautifulSoup, str]:
        _, params = parse_input_url(article.url)
        params.update({k: v for k, v in self.auth.items() if k != "poc_sid"})
        parsed = urlparse(article.url)
        url = urlunparse(parsed._replace(query=urlencode(params)))
        response = self._get(url)
        if "/mp/wappoc_appmsgcaptcha" in response.url:
            raise DownloadError("微信返回访问验证页；请使用自己的有效会话后重试")
        response.encoding = response.encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        content = soup.select_one("#js_content") or soup.select_one("#js_image_content")
        if not content:
            raise DownloadError("未找到文章正文；可能需要更新会话参数，或此文章格式暂不支持")
        title_tag = soup.select_one("#activity-name") or soup.find("h1")
        if title_tag:
            article.title = title_tag.get_text(" ", strip=True) or article.title
        author_tag = soup.select_one("#js_name")
        if author_tag:
            article.author = author_tag.get_text(" ", strip=True)
        return soup, str(content)

    def image(self, url: str) -> bytes:
        response = self._get(url, headers={"Referer": BASE + "/"})
        if len(response.content) > 25 * 1024 * 1024:
            raise DownloadError("单张图片超过 25 MB")
        return response.content


def matches(article: Article, options: Options) -> bool:
    if options.keyword and options.keyword.casefold() not in article.title.casefold():
        return False
    if article.published:
        day = article.published.date()
        if options.start_date and day < options.start_date:
            return False
        if options.end_date and day > options.end_date:
            return False
    return True


def save_article(client: WeChatClient, article: Article, options: Options,
                 stop: threading.Event, log: Callable[[str], None] = lambda _: None) -> list[Path]:
    from .enrich import comments_html, fetch_comments, save_media
    from .exporters import export_article

    if stop.is_set():
        raise Stopped()
    soup, raw_content = client.fetch_article(article)
    content = BeautifulSoup(raw_content, "html.parser")
    # Offline exports are opened as local documents. Discard active page code
    # while keeping the article's text, links, layout and images.
    for element in content.find_all(['script', 'iframe', 'object', 'embed', 'form', 'input', 'button']):
        element.decompose()
    for element in content.find_all(True):
        for attribute in list(element.attrs):
            if attribute.lower().startswith('on') or attribute.lower() == 'srcdoc':
                del element.attrs[attribute]
        for attribute in ('href', 'src'):
            value = element.get(attribute)
            if isinstance(value, str) and value.strip().lower().startswith(('javascript:', 'data:text/html')):
                del element.attrs[attribute]
    # WeChat reveals the article with JavaScript after loading. The offline copy
    # has no such script, so its initial hidden state must be removed.
    for root in content.select("#js_content, #js_image_content"):
        root.attrs.pop("hidden", None)
        style = root.get("style", "")
        style = re.sub(r"(?:^|;)\s*(?:visibility|opacity|display)\s*:[^;]*(?=;|$)", "", style,
                       flags=re.I).strip(" ;")
        if style:
            root["style"] = style
        else:
            root.attrs.pop("style", None)
    stamp = article.published.strftime("%Y-%m-%d_") if article.published else ""
    stem = safe_name(stamp + article.title, 85) + '_' + hashlib.sha256(article.url.encode()).hexdigest()[:8]
    folder = options.output / stem
    folder.mkdir(parents=True, exist_ok=True)
    formats = options.formats or (options.fmt,)
    if any(fmt not in SUPPORTED_FORMATS for fmt in formats):
        raise DownloadError("选择了不支持的导出格式")
    targets = [folder / (stem + "." + fmt) for fmt in formats]
    if options.skip_existing and all(target.exists() for target in targets):
        return targets
    for index, img in enumerate(content.find_all("img"), 1):
        src = img.get("data-src") or img.get("data-original") or img.get("src")
        if not src:
            continue
        src = urljoin(BASE, src)
        if not src.startswith(("http://", "https://")):
            continue
        if options.images:
            if stop.is_set():
                raise Stopped()
            parsed = urlparse(src)
            ext = Path(parsed.path).suffix.lower()
            if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                ext = ".jpg"
            relative = f"images/{index:03d}{ext}"
            try:
                data = client.image(src)
                image_path = folder / relative
                image_path.parent.mkdir(exist_ok=True)
                image_path.write_bytes(data)
                img["src"] = relative
            except DownloadError:
                img["src"] = src
        else:
            img["src"] = src
        for attr in ("data-src", "data-original"):
            img.attrs.pop(attr, None)

    if options.cover or options.audio or options.video:
        enabled = {kind for kind in ("cover", "audio", "video") if getattr(options, kind)}
        media_files = save_media(client, soup, folder, enabled, log)
        playable = [path for path in media_files if path.name.startswith(("audio_", "video_"))]
        if playable:
            section = soup.new_tag("section")
            section["class"] = "saved-media"
            heading = soup.new_tag("h2")
            heading.string = "已保存媒体"
            section.append(heading)
            for path in playable:
                tag = soup.new_tag("audio" if path.name.startswith("audio_") else "video")
                tag["controls"] = ""
                tag["preload"] = "metadata"
                tag["src"] = path.name
                if tag.name == "video":
                    tag["style"] = "max-width:100%;height:auto"
                section.append(tag)
            content.append(section)
    extra_html = ""
    if options.comments:
        try:
            extra_html = comments_html(fetch_comments(client, soup))
        except DownloadError as exc:
            log(f"评论获取失败：{exc}")
    for fmt, target in zip(formats, targets):
        if options.skip_existing and target.exists():
            continue
        try:
            export_article(fmt, target, article.title, article.url, article.author,
                           content, extra_html)
        except (ValueError, ImportError) as exc:
            raise DownloadError(f"{fmt} 导出失败：{exc}") from exc
    return targets


def run_download(client: WeChatClient, source_url: str, options: Options,
                 stop: threading.Event, log: Callable[[str], None], mode: str = "history") -> dict:
    if options.batch_size < 1 or options.retries < 0:
        raise DownloadError("每批链接数至少为 1，重试次数不能为负数")
    if mode != "links":
        biz, _ = parse_input_url(source_url)
        if mode == "history" and not biz:
            biz = client.resolve_biz(source_url)
    options.output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    count, failed = 0, 0
    if mode == "single":
        articles: Iterator[Article] = iter([Article("文章", source_url)])
    elif mode == "links":
        urls = parse_article_links(source_url)
        articles = iter(Article("文章", url) for url in urls)
        log(f"待处理 {len(urls)} 条链接，分为 {(len(urls) + options.batch_size - 1) // options.batch_size} 批")
    elif mode == "album":
        from .enrich import album_articles
        articles = iter(album_articles(client, source_url, options.max_pages))
    else:
        articles = client.history(biz, stop, options.max_pages, options.delay)
    seen_urls: set[str] = set()
    processed = 0
    try:
        for article in articles:
            if stop.is_set():
                break
            if article.url in seen_urls:
                continue
            seen_urls.add(article.url)
            if not matches(article, options):
                continue
            if mode == "links" and processed % options.batch_size == 0:
                log(f"开始第 {processed // options.batch_size + 1} 批")
            processed += 1
            row = {"title": article.title, "url": article.url,
                   "published": article.published.isoformat() if article.published else "",
                   "author": article.author, "file": "", "status": ""}
            for attempt in range(options.retries + 1):
                try:
                    paths = save_article(client, article, options, stop, log)
                    row.update(title=article.title, author=article.author,
                               file="; ".join(str(path) for path in paths), status="ok")
                    count += 1
                    log(f"已保存：{article.title}（{len(paths)} 种格式）")
                    break
                except Stopped:
                    stop.set()
                    break
                except (DownloadError, OSError, ValueError) as exc:
                    if attempt < options.retries and not stop.is_set():
                        log(f"第 {attempt + 1} 次失败，稍后重试：{article.url}：{exc}")
                        if stop.wait(max(1, options.delay)):
                            break
                    else:
                        row["status"] = f"失败：{exc}"
                        failed += 1
                        log(f"失败：{article.url}：{exc}")
            if stop.is_set():
                break
            rows.append(row)
            if mode != "single" and stop.wait(max(0, options.delay)):
                break
    finally:
        if rows:
            csv_path = options.output / f"articles_{datetime.now():%Y%m%d_%H%M%S_%f}.csv"
            with csv_path.open("w", newline="", encoding="utf-8-sig") as file:
                writer = csv.DictWriter(file, fieldnames=["title", "url", "published", "author", "file", "status"])
                writer.writeheader()
                writer.writerows(rows)
            log(f"清单：{csv_path}")
    return {"saved": count, "failed": failed, "stopped": stop.is_set(),
            "output": str(options.output),
            "manifest": str(csv_path) if rows else ""}
