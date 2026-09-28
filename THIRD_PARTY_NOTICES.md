# Reference and third-party notices

## WCDB configuration key recovery

The WCDB Config.Cipher structure and encoded key-literal recovery in wxdump.py
are adapted from fanyuantaier/wechatauto-replica (Apache-2.0):
https://github.com/fanyuantaier/wechatauto-replica/blob/main/wechatauto/db.py
The adaptation uses Frida reads, salt-based database association, and SQLCipher
validation. The upstream license is retained in licenses/wechatauto-replica-LICENSE.txt.

The Shiguang interface and archive layer are developed in this folder. Product features and WeChat 4.x message/media layout research reference MemoTrace:
The bundled `mywxplus` package and its modified `wechatauto-replica` snapshot are Apache-2.0 licensed. Their license and attribution are retained in `licenses/mywxplus-LICENSE.txt` and `licenses/mywxplus-NOTICE.txt`. Source was integrated from the user's local `mywxplus` project on 2026-09-27.
https://github.com/shixiaogaoya/MemoTrace

Relevant reference files: wxManager/db_v4/hardlink.py, media.py, head_image.py; parser/wechat_v4.py and packed_info_data*.proto; decrypt/decrypt_dat.py. Their MIT notice is retained below. No upstream UI assets or branding are bundled.

V2 Windows image-key validation was also cross-checked against the primary implementation/documentation at https://github.com/jackwener/wx-cli-again/tree/main/src/attachment/image_key. The Python implementation here uses Windows read-only process-memory APIs and verifies candidates against local image headers. No Rust source is included.

Dependencies retain their respective licenses: PySide6/Qt, SQLCipher, Frida, Pillow, PyCryptodome, python-docx, qrcode, pysilk-mod, imageio-ffmpeg and jieba. imageio-ffmpeg bundles its own FFmpeg executable and notices. Dependency licenses must be preserved if distributing a packaged executable.

## MemoTrace MIT notice

MIT License

Copyright (c) 2024 SiYuan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
## RevokeMsgPatcher integration

The legacy current-process patch references the single-byte Weixin >= 4.1.12.0 anti-revoke rule documented in RevokeMsgPatcher (PatchVersion 20260816):
https://github.com/huiyadanli/RevokeMsgPatcher/blob/master/RevokeMsgPatcher.Assistant/Data/2.1/patch.json
Upstream project: huiyadanli/RevokeMsgPatcher (GPL-3.0), pinned at commit `8360fe2af70ef908dfe514eb78ca75129eacc5cc`; Weixin rule provenance is BetterWX as credited upstream. Its complete source checkout, rule catalog, and GPL-3.0 license are now included under `third_party/RevokeMsgPatcher`. `wxdesk/revoke_disk.py` reads the upstream rule catalog and applies the Weixin anti-revoke file patch only after all Weixin processes exit, with an original-file backup and restore path. No upstream executable is bundled. Distribution of this integrated source must comply with GPL-3.0 obligations. The old current-process patch remains disabled because a live test reported that an incoming message still disappeared after recall even though the memory byte was written. File patching is a different path; its real recall effect still requires testing after Weixin restarts.

## WeChatBot_WXAUTO_SE reference

The user's local `WeChatBot_WXAUTO_SE-3.28.zip` is GPL-3.0-or-later and its own README says it supports WeChat 3.9, not 4.0 and above. Its prompt-per-contact, batching, time-awareness and image-input ideas informed independent implementation in `wxdesk/automation.py` and `wxdesk/llm.py`. No bot source, proprietary wxautox wheel, prompt collection, or bundled emoji assets are copied into this project.

## wx_channels_download research reference

Video Channels share-link behavior was compared with `ltaoo/wx_channels_download`:
https://github.com/ltaoo/wx_channels_download
That project uses a Tencent Yuanbao session to exchange a share URL for a playable token, and its legacy mode installs a local root certificate and system proxy. This project independently implements only the HTTPS request flow. Its dedicated Qt WebEngine profile retains the user's own Yuanbao login locally; request cookies are passed to the downloader in process memory and are not written to task records or logs. This project does not include upstream source, proxy, certificate code, or binaries. The referenced project is distributed under MIT with the Commons Clause condition; no code from it is redistributed here.
