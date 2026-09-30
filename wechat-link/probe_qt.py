import argparse
import os
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QLabel, QTextEdit, QVBoxLayout, QWidget


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    output = Path(args.output)
    app = QApplication([])
    window = QWidget()
    window.setWindowTitle("wechat-link Qt input probe")
    window.resize(800, 500)
    layout = QVBoxLayout(window)
    layout.addWidget(QLabel("中文输入 / 剪贴板测试；不会发送微信消息"))
    editor = QTextEdit()
    layout.addWidget(editor)
    editor.textChanged.connect(lambda: output.write_text(editor.toPlainText(), encoding="utf-8"))
    window.show()
    editor.setFocus()
    app.exec_()


if __name__ == "__main__":
    main()
