from keeps.clipboard import make_mime_data

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d"
    "29dc0000001949444154789c6378e0e0605070019364c02a0a241906a50e00"
    "2ad55a01f9c77e7c0000000049454e44ae426082"
)


def test_image_clipboard_payload_offers_png_mime_directly():
    mime = make_mime_data({"image/png": PNG_1X1})

    assert mime.hasImage()
    assert "image/png" in mime.formats()
