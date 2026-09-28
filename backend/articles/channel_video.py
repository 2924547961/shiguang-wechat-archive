"""Download a public Channels video when the official web preview exposes it."""

from __future__ import annotations

from datetime import datetime
from html import escape
import json
from pathlib import Path
import random
import re
import threading
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from .downloader import DownloadError, USER_AGENT, safe_name


PREVIEW_API = "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
SUPPORTED_HOSTS = {"weixin.qq.com", "channels.weixin.qq.com"}
MEDIA_SUFFIXES = (".qq.com", ".qpic.cn", ".weixin.qq.com")


def _short_id(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in SUPPORTED_HOSTS:
        raise DownloadError("请输入微信视频号 HTTPS 分享链接")
    if parsed.hostname == "weixin.qq.com":
        match = re.fullmatch(r"/sph/([A-Za-z0-9_-]{4,128})/?", parsed.path)
        if match:
            return match.group(1)
    if parsed.hostname == "channels.weixin.qq.com" and parsed.path.endswith("/sph"):
        candidate = (parse_qs(parsed.query).get("id") or [""])[-1]
        if re.fullmatch(r"[A-Za-z0-9_-]{4,128}", candidate):
            return candidate
    raise DownloadError("无法从链接中识别视频号短码")


def _https_url(value: object) -> str:
    text = str(value or "").replace("http://", "https://", 1)
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or not host.endswith(MEDIA_SUFFIXES):
        return ""
    return text


def _stream_url(feed: dict) -> str:
    candidates = [
        (feed.get("h264VideoInfo") or {}).get("videoUrl"),
        feed.get("videoUrl"),
        (feed.get("h265VideoInfo") or {}).get("videoUrl"),
    ]
    return next((url for url in map(_https_url, candidates) if url), "")


def _feed_info(session: requests.Session, body: dict, referer: str) -> dict:
    rid = f"{int(datetime.now().timestamp()):x}-" + ''.join(random.choice('0123456789abcdef') for _ in range(8))
    response = session.post(PREVIEW_API, params={"_rid": rid,
        "_pageUrl": "https://channels.weixin.qq.com/finder-preview/pages/feed"},
        json=body, headers={"Referer": referer}, timeout=(10, 30))
    response.raise_for_status()
    payload = response.json()
    if payload.get("errCode") not in (0, "0", None):
        raise DownloadError(f"视频号接口返回错误：{payload.get('errMsg') or payload.get('errCode')}")
    return payload


def _yuanbao_feed(session: requests.Session, source: str, cookie: str) -> dict:
    headers = {"Accept": "application/json, text/plain, */*", "Origin": "https://yuanbao.tencent.com",
               "Referer": "https://yuanbao.tencent.com/", "X-Requested-With": "XMLHttpRequest",
               "X-Source": "web", "X-Platform": "windows", "X-Language": "zh-CN", "Cookie": cookie}
    response = session.post("https://yuanbao.tencent.com/api/weixin/get_parse_result",
                            json={"type": "video_channel_url", "url": source, "scene": 1},
                            headers=headers, timeout=(10, 30))
    if response.status_code in (401, 403):
        raise DownloadError("腾讯元宝登录态已失效，请点击“登录腾讯元宝”重新登录")
    response.raise_for_status()
    result = response.json()
    parsed = result.get("data") or {}
    playable = urlparse(parsed.get("playable_url") or "")
    query = parse_qs(playable.query)
    token = (query.get("token") or [""])[-1]
    export_id = (query.get("eid") or [parsed.get("wx_export_id") or ""])[-1]
    if not export_id:
        raise DownloadError("腾讯元宝没有返回视频号可播放标识")
    referer = ("https://channels.weixin.qq.com/finder-preview/pages/feed?entry_card_type=48"
               f"&comment_scene=39&appid=0&token={token}&entry_scene=0&eid={export_id}")
    return _feed_info(session, {"baseReq": {"generalToken": token}, "exportId": export_id}, referer)


def _download(session: requests.Session, url: str, target: Path,
              stop: threading.Event, limit: int = 2 * 1024 * 1024 * 1024) -> int:
    size = 0
    try:
        response = None
        for _ in range(6):
            if not _https_url(url):
                raise DownloadError("视频资源地址不受支持")
            response = session.get(url, stream=True, timeout=(10, 45), allow_redirects=False)
            if not response.is_redirect:
                break
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise DownloadError("视频资源跳转缺少目标地址")
            url = urljoin(url, location)
        if response is None or response.is_redirect:
            raise DownloadError("视频资源跳转次数过多")
        with response:
            response.raise_for_status()
            final = _https_url(response.url)
            if not final:
                raise DownloadError("视频资源跳转到了不受支持的地址")
            with target.open("wb") as file:
                for chunk in response.iter_content(512 * 1024):
                    if stop.is_set():
                        raise DownloadError("下载已停止")
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise DownloadError("视频文件超过 2 GB")
                    file.write(chunk)
    except requests.RequestException as exc:
        target.unlink(missing_ok=True)
        raise DownloadError(f"视频下载失败：{exc}") from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return size


def download_channel_video(source: str, output: Path, stop: threading.Event, log,
                           yuanbao_cookie: str = "") -> dict:
    short_id = _short_id(source)
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Origin": "https://channels.weixin.qq.com",
        "Referer": f"https://channels.weixin.qq.com/finder-preview/pages/sph?id={short_id}",
    })
    try:
        payload = _feed_info(session, {"baseReq": {"generalToken": ""}, "shortUri": short_id},
                             session.headers["Referer"])
    except (requests.RequestException, ValueError) as exc:
        raise DownloadError(f"视频号预览信息读取失败：{exc}") from exc
    data = payload.get("data") or {}
    feed = data.get("feedInfo") or {}
    author = data.get("authorInfo") or {}
    video_url = _stream_url(feed)
    if not video_url and yuanbao_cookie:
        log("官方分享页未返回视频流，正在使用本机提供的腾讯元宝登录态解析")
        try:
            payload = _yuanbao_feed(session, source, yuanbao_cookie)
        except (requests.RequestException, ValueError) as exc:
            raise DownloadError(f"腾讯元宝解析失败：{exc}") from exc
        data = payload.get("data") or {}
        feed = data.get("feedInfo") or {}
        author = data.get("authorInfo") or author
        video_url = _stream_url(feed)
    if not video_url:
        message = (data.get("errMsg") or {}).get("title")
        suffix = "请在视频号模式登录腾讯元宝后重试。" if not yuanbao_cookie else "当前登录态仍未取得视频流。"
        raise DownloadError((message or "这条分享没有向网页端提供可下载的视频流。") + suffix)
    title = str(feed.get("description") or "视频号视频").strip()
    created = feed.get("createtime")
    stamp = ""
    try:
        stamp = datetime.fromtimestamp(int(created)).strftime("%Y-%m-%d_") if created else ""
    except (TypeError, ValueError, OSError):
        pass
    folder = output / (safe_name(stamp + title, 85) + "_" + short_id[:12])
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "video.mp4"
    log("正在下载视频号原始视频")
    size = _download(session, video_url, video, stop)
    cover_url = _https_url(feed.get("coverUrl"))
    cover = folder / "cover.jpg"
    if cover_url and not stop.is_set():
        try:
            _download(session, cover_url, cover, stop, 50 * 1024 * 1024)
        except DownloadError as exc:
            log(f"封面保存失败：{exc}")
    metadata = {
        "title": title,
        "author": str(author.get("nickname") or ""),
        "published": int(created or 0),
        "source": source,
        "video": video.name,
        "cover": cover.name if cover.is_file() else "",
        "size": size,
    }
    (folder / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), "utf-8")
    cover_attr = ' poster="cover.jpg"' if cover.is_file() else ""
    (folder / "观看.html").write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>" + escape(title) + "</title>"
        "<style>body{max-width:900px;margin:40px auto;padding:0 20px;font:16px/1.7 sans-serif;background:#f6f7f3;color:#24352b}"
        "video{width:100%;max-height:75vh;background:#111;border-radius:14px}</style>"
        f"<h1>{escape(title)}</h1><p>{escape(metadata['author'])}</p><video controls preload=\"metadata\"{cover_attr} src=\"video.mp4\"></video>",
        "utf-8")
    log(f"已保存：{title}（视频号视频）")
    return {"saved": 1, "failed": 0, "stopped": False, "output": str(folder),
            "manifest": str(folder / "metadata.json"), "video": str(video)}
