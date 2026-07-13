"""Lazy-qrcode dialog for Ф18."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout


def qr_matrix(text: str) -> list[list[bool]]:
    import qrcode  # lazy: opening ordinary popup paths does not import it

    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
    code.add_data(text)
    code.make(fit=True)
    return code.get_matrix()


def qr_image(text: str, scale: int = 8) -> QImage:
    matrix = qr_matrix(text)
    size = len(matrix)
    image = QImage(size * scale, size * scale, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    black = QColor("black").rgb()
    for y, row in enumerate(matrix):
        for x, filled in enumerate(row):
            if not filled:
                continue
            for py in range(y * scale, (y + 1) * scale):
                for px in range(x * scale, (x + 1) * scale):
                    image.setPixel(px, py, black)
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
