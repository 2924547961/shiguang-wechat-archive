"""Verify the actual Qt WebEngine desktop runtime, without touching WeChat."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wxdesk.desktop import prepare_qt_environment
prepare_qt_environment()
from PySide6.QtCore import QTimer,QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

application=QApplication([])
errors=[]
class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self,level,message,line,source):
        if level==self.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            errors.append(message)

view=QWebEngineView();page=Page(view);view.setPage(page)
page.loadingChanged.connect(lambda info: print('LOAD',info.status(),info.errorCode(),info.errorString(),flush=True))
page.renderProcessTerminated.connect(lambda status,code: print('RENDER_PROCESS',status,code,flush=True))
view.resize(1280,850);view.setWindowTitle('拾光 · 界面验证（演示）')
view.move(40,40);view.show()
def inspect():
    def ready(result):
        print('NATIVE_READY',result,'JS_ERRORS',len(errors),flush=True)
        if result:
            view.grab().save(sys.argv[2])
        if errors:
            print(errors,flush=True)
        application.exit(0 if result and not errors else 1)
    page.runJavaScript("document.querySelector('.page-heading h1')?.textContent.includes('我的微信档案') && !!document.querySelector('.account-focus')",ready)
def loaded(ok):
    if ok:QTimer.singleShot(1600,inspect)
    else:
        print('NATIVE_LOAD_FAILED',flush=True)
        page.toHtml(lambda value: print('PAGE',value[:1200],flush=True))
        QTimer.singleShot(500,lambda:application.exit(1))
page.loadFinished.connect(loaded);view.setUrl(QUrl(sys.argv[1]))
QTimer.singleShot(15000,lambda:application.exit(2))
raise SystemExit(application.exec())
