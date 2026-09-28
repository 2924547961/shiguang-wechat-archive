from __future__ import annotations
import argparse
import os
import sys
import threading
from pathlib import Path

_dll_handles = []


def prepare_qt_environment():
    """Make Qt DLLs visible to WebEngine's child process in Conda and pip installs."""
    if os.name != "nt":
        return
    import importlib.util
    spec = importlib.util.find_spec("PySide6")
    roots = [Path(sys.prefix) / "Library" / "bin", Path(sys.prefix)]
    if spec and spec.origin:
        roots.insert(0, Path(spec.origin).parent)
    roots = [p for p in roots if p.is_dir()]
    os.environ["PATH"] = os.pathsep.join([str(p) for p in roots] + [os.environ.get("PATH", "")])
    for root in roots:
        if hasattr(os, "add_dll_directory"):
            _dll_handles.append(os.add_dll_directory(str(root)))


def main():
    prepare_qt_environment()
    parser = argparse.ArgumentParser(description="拾光 · 微信本地档案")
    parser.add_argument("--server", action="store_true", help="只启动本地预览服务")
    parser.add_argument("--demo", action="store_true", help="使用明确标注的虚构演示数据")
    parser.add_argument("--state", help="自定义应用数据目录")
    args = parser.parse_args()
    from .common import STATIC
    from .storage_location import archive_choice, save_archive_choice
    state, first_run = archive_choice(args.state)
    gui_app = None
    if not args.server:
        from PySide6.QtWidgets import QApplication, QFileDialog
        gui_app = QApplication(sys.argv[:1])
        gui_app.setApplicationName("拾光")
        gui_app.setOrganizationName("ShiguangLocal")
        if first_run and not args.demo:
            state.parent.mkdir(parents=True, exist_ok=True)
            selected = QFileDialog.getExistingDirectory(None, "选择拾光归档目录（不是微信原始数据目录）", str(state.parent))
            state = Path(selected).resolve() if selected else state
            state.mkdir(parents=True, exist_ok=True)
            save_archive_choice(state)
    if args.demo:
        from .demo import make_demo
        state = state / "demo" if not args.state else state
        make_demo(state)
    from .server import Application, LocalServer
    application = Application(state=state, demo=args.demo); server = LocalServer(application)
    application.start_watcher()
    application.automation.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if args.server:
        print(server.url, flush=True)
        try: threading.Event().wait()
        except KeyboardInterrupt: application.close(); server.shutdown()
        return 0
    from PySide6.QtCore import QObject, Signal, Slot, QUrl
    from PySide6.QtGui import QDesktopServices, QIcon, QPixmap, QPainter, QColor, QFont
    from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    app = gui_app
    class Bridge(QObject):
        folderSelected = Signal(str, str)
        openRequested = Signal(str)
        @Slot(str, str)
        def chooseFolder(self, kind, current):
            title = {"output_dir": "选择导出目录", "data_root": "选择微信数据目录", "emoji_dir": "选择自备表情图片文件夹", "article_output": "选择文章保存目录", "article_cache": "选择自己的微信缓存目录", "archive_dir": "选择拾光归档目录"}.get(kind, "选择文件夹")
            folder = QFileDialog.getExistingDirectory(window, title, current)
            if folder: self.folderSelected.emit(kind, folder)
        @Slot(str)
        def openLocal(self, target): QDesktopServices.openUrl(QUrl.fromLocalFile(target))
        @Slot()
        def quitApp(self): window.close()
    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, nav_type, is_main):
            if url.scheme() in {"http", "https"} and url.host() == "127.0.0.1" and url.port() == server.server_port: return True
            if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in {"http", "https"}: QDesktopServices.openUrl(url)
            return url.scheme() == "about"
    class Window(QMainWindow):
        def closeEvent(self, event):
            running = any(j["status"] == "running" and not j.get("automatic") for j in application.jobs.values())
            if running and QMessageBox.question(self, "任务仍在进行", "关闭窗口会取消当前任务，已完成的归档会保留。现在关闭？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                event.ignore(); return
            application.close(); server.shutdown(); server.server_close(); event.accept()
    window = Window(); window.setWindowTitle("拾光 · 微信本地档案" + (" · 演示模式" if args.demo else ""))
    available = app.primaryScreen().availableGeometry()
    window.setMinimumSize(min(1100, available.width() - 40), min(740, available.height() - 60))
    window.resize(min(1440, available.width() - 70), min(940, available.height() - 90))
    icon = QPixmap(64, 64); icon.fill(QColor("#225c48")); painter = QPainter(icon)
    painter.setPen(QColor("#fffdf6")); painter.setFont(QFont("Microsoft YaHei", 31, QFont.Weight.Bold)); painter.drawText(icon.rect(), 0x84, "拾"); painter.end()
    window.setWindowIcon(QIcon(str(STATIC / 'app.ico'))); view = QWebEngineView(window)
    profile = QWebEngineProfile(view); profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
    page = Page(profile, view); view.setPage(page)
    def open_new_window(request):
        url = request.requestedUrl()
        if request.isUserInitiated() and url.scheme() in {"http", "https"}:
            QDesktopServices.openUrl(url)
    page.newWindowRequested.connect(open_new_window)
    def save_download(download):
        suggested = str(Path(download.downloadDirectory()) / Path(download.downloadFileName()).name)
        target, _ = QFileDialog.getSaveFileName(window, "保存附件", suggested)
        if target:
            destination = Path(target)
            download.setDownloadDirectory(str(destination.parent))
            download.setDownloadFileName(destination.name)
            download.accept()
        else:
            download.cancel()
    profile.downloadRequested.connect(save_download)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, True)
    bridge = Bridge(window); bridge.openRequested.connect(bridge.openLocal); application.open_callback = bridge.openRequested.emit
    channel = QWebChannel(page); channel.registerObject("native", bridge); page.setWebChannel(channel)
    window.setCentralWidget(view); view.setUrl(QUrl(server.url)); window.show()
    return app.exec()
