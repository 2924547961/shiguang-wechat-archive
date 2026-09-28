"""Exercise native link and download handlers with fictional data only."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wxdesk.desktop import prepare_qt_environment, main
prepare_qt_environment()
from PySide6.QtCore import QTimer, QPoint, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtTest import QTest

folder = tempfile.TemporaryDirectory(prefix='shiguang-native-')
target = Path(folder.name) / 'saved.txt'
opened = []
QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toString()) or True)
QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ''))
original_exec = QApplication.exec
state = {'stage': 0, 'ticks': 0}

def tick():
    state['ticks'] += 1
    windows = QApplication.topLevelWidgets()
    window = next((w for w in windows if w.windowTitle().startswith('拾光')), None)
    view = window.findChild(QWebEngineView) if window else None
    if state['ticks'] > 40:
        print('NATIVE_ACTIONS_TIMEOUT', state, flush=True)
        QApplication.instance().exit(1)
        return
    if not view:
        return
    if state['stage'] == 0:
        def ready(ok):
            if ok and state['stage'] == 0:
                state['stage'] = 1
                script = """const testLinks=document.createElement('div');
                testLinks.style='position:fixed;left:0;top:0;z-index:99999;background:white;width:400px;height:100px';
                testLinks.innerHTML='<a style="display:block;height:40px" target="_blank" href="https://example.invalid/native-test">外部链接</a><a style="display:block;height:40px" download="fixture.txt" href="/media/'+state.selected+'/assets/'+encodeURIComponent('行程清单.txt')+'">保存附件</a>';
                document.body.appendChild(testLinks);"""
                view.page().runJavaScript(script)
        view.page().runJavaScript("typeof state!=='undefined' && !!state?.selected", ready)
    elif state['stage'] == 1:
        state['stage'] = 2
        QTest.mouseClick(view.focusProxy(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 20))
    elif state['stage'] == 2 and opened:
        state['stage'] = 3
        QTest.mouseClick(view.focusProxy(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(40, 60))
    elif state['stage'] == 3 and target.exists():
        passed = opened == ['https://example.invalid/native-test'] and '虚构演示附件' in target.read_text('utf-8')
        print('NATIVE_EXTERNAL_LINK_AND_DOWNLOAD', passed, flush=True)
        window.close()
        QApplication.instance().exit(0 if passed else 1)

def run_loop(self):
    timer = QTimer(self)
    timer.timeout.connect(tick)
    timer.start(500)
    return original_exec()

QApplication.exec = run_loop
sys.argv = ['wxdump_gui.py', '--demo', '--state', str(Path(folder.name) / 'state')]
raise SystemExit(main())
