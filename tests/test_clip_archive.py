import pytest

from keeps.clip_archive import ArchiveClip, content_export, decode_archive, encode_archive
from keeps.store import Store


def test_archive_roundtrip_preserves_every_mime_and_clip_metadata():
    clips = [
        ArchiveClip(
            kind="html",
            mime_data={
                "text/plain": b"hello",
                "text/html": b"<b>hello</b>",
                "application/x-editor-state": b"\x00\xff",
            },
            pinned=True,
            alias="Greeting",
        ),
        ArchiveClip(
            kind="image",
            mime_data={"image/png": b"\x89PNG\r\n\x1a\n", "image/svg+xml": b"<svg/>"},
        ),
    ]

    payload = encode_archive(clips)

    assert payload[:2] == b"\x1f\x8b"
    assert decode_archive(payload) == clips


def test_content_export_returns_the_actual_image_bytes_and_suffix():
    assert content_export("image", {"image/png": b"png-bytes"}) == (".png", b"png-bytes")


def test_content_export_returns_textual_canonical_format():
    assert content_export("html", {"text/plain": b"plain", "text/html": b"<b>x</b>"}) == (
        ".html",
        b"<b>x</b>",
    )


@pytest.mark.parametrize(
    "payload",
    [b"not gzip", b"\x1f\x8b\x08\x00broken"],
)
def test_archive_rejects_invalid_payload(payload):
    with pytest.raises(ValueError):
        decode_archive(payload)


def test_imported_duplicate_does_not_move_existing_clip_to_the_top(tmp_path):
    store = Store(tmp_path / "keeps.db")
    duplicate_id = store.add("text", {"text/plain": b"already here"})
    newest_id = store.add("text", {"text/plain": b"newest"})
    use_count = next(clip.use_count for clip in store.all() if clip.id == duplicate_id)

    result_id, inserted = store.import_clip(
        ArchiveClip("text", {"text/plain": b"already here"})
    )

    assert (result_id, inserted) == (duplicate_id, False)
    assert [clip.id for clip in store.all()] == [newest_id, duplicate_id]
    assert next(clip.use_count for clip in store.all() if clip.id == duplicate_id) == use_count
    store.close()


def test_imported_new_clip_preserves_pinned_and_alias(tmp_path):
    store = Store(tmp_path / "keeps.db")

    clip_id, inserted = store.import_clip(
        ArchiveClip(
            "text",
            {"text/plain": b"from another machine", "application/x-note": b"meta"},
            pinned=True,
            alias="Imported",
        )
    )

    clip = store.all()[0]
    assert inserted is True
    assert clip.id == clip_id
    assert clip.pinned is True
    assert clip.alias == "Imported"
    assert store.get_data(clip_id)["application/x-note"] == b"meta"
    store.close()


def test_export_then_import_into_a_clean_database_reproduces_all_formats(tmp_path):
    source = Store(tmp_path / "source.db")
    source_id = source.add(
        "html",
        {
            "text/plain": b"formatted text",
            "text/html": b"<strong>formatted text</strong>",
            "application/x-editor-state": b"\x00state\xff",
        },
    )
    source.set_pinned(source_id, True)
    source.set_alias(source_id, "Saved selection")
    source_clip = source.all()[0]
    archive = encode_archive(
        [
            ArchiveClip(
                source_clip.kind,
                source.get_data(source_id),
                pinned=source_clip.pinned,
                alias=source_clip.alias,
            )
        ]
    )
    source.close()

    target = Store(tmp_path / "target.db")
    imported_id, inserted = target.import_clip(decode_archive(archive)[0])

    imported_clip = target.all()[0]
    assert inserted is True
    assert imported_clip.id == imported_id
    assert imported_clip.pinned is True
    assert imported_clip.alias == "Saved selection"
    assert target.get_data(imported_id) == {
        "text/plain": b"formatted text",
        "text/html": b"<strong>formatted text</strong>",
        "application/x-editor-state": b"\x00state\xff",
    }
    target.close()
