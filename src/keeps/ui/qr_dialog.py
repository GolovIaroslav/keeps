"""Lazy-qrcode dialog for Ф18."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout


def qr_matrix(text: str) -> list[list[bool]]:
    import qrcode  # lazy: opening ordinary popup paths does not import it

    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def qr_image(text: str, scale: int = 8, max_size: int = 720) -> QImage:
    matrix = qr_matrix(text)
    size = len(matrix)
    scale = max(1, min(scale, max_size // size))
    image = QImage(size * scale, size * scale, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            if filled:
                painter.drawRect(x * scale, y * scale, scale, scale)
    painter.end()
    return image


class QrDialog(QDialog):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("QR code"))
        layout = QVBoxLayout(self)
        label = QLabel()
        label.setPixmap(QPixmap.fromImage(qr_image(text)))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
