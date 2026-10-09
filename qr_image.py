"""Pixel-aligned QR rendering, with a four-module quiet zone on every side."""
import qrcode
from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QWidget


def qr_matrix(payload):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    return qr.get_matrix()


def matrix_image(matrix, module_size=8):
    size = len(matrix) * module_size
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.white)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                painter.fillRect(x * module_size, y * module_size, module_size, module_size, Qt.GlobalColor.black)
    painter.end()
    return image


class QRWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.matrix = None
        self.message = '点击“生成二维码”后，使用学习通扫码'
        self.setMinimumSize(290, 290)
        self.setAccessibleName('学习通登录二维码')

    def sizeHint(self):
        return QSize(360, 360)

    def set_payload(self, payload):
        self.matrix = qr_matrix(payload)
        self.update()

    def clear(self, message='二维码已失效，请重新生成'):
        self.matrix = None
        self.message = message
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.white)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        if self.matrix:
            ratio = self.devicePixelRatioF()
            # Integer physical pixels per module, even at 125% / 150% display scaling.
            scale = int(min(self.width(), self.height()) * ratio) // len(self.matrix)
            if scale >= 1:
                image = matrix_image(self.matrix, scale)
                image.setDevicePixelRatio(ratio)
                x = round((self.width() * ratio - image.width()) / 2) / ratio
                y = round((self.height() * ratio - image.height()) / 2) / ratio
                painter.drawImage(QPointF(x, y), image)
        else:
            painter.setPen(QColor('#64748b'))
            painter.drawText(self.rect().adjusted(20, 20, -20, -20),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)
        painter.end()
