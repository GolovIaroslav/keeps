from keeps.ai import models


def _spec(n_files=1):
    files = tuple(
        models.ModelFile(
            repo=f"org/repo{i}",
            path_in_repo="inference.onnx",
            sha256="0" * 64,
            size_bytes=10,
        )
        for i in range(n_files)
    )
    return models.ModelSpec(name="test-model", label="Test model", files=files)


def test_not_downloaded_when_files_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    spec = _spec()
    assert models.is_downloaded(spec) is False
    assert models.status(spec, loaded=False) == models.ModelStatus.NOT_DOWNLOADED


def test_downloaded_when_all_files_present_with_right_size(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    spec = _spec(n_files=2)
    for f in spec.files:
        dest = models.file_dest(spec, f)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"0" * f.size_bytes)

    assert models.is_downloaded(spec) is True
    assert models.status(spec, loaded=False) == models.ModelStatus.DOWNLOADED
    assert models.status(spec, loaded=True) == models.ModelStatus.LOADED


def test_partial_download_not_counted_as_downloaded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    spec = _spec(n_files=2)
    only_file = spec.files[0]
    dest = models.file_dest(spec, only_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"0" * only_file.size_bytes)

    assert models.is_downloaded(spec) is False


def test_wrong_size_not_counted_as_downloaded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    spec = _spec()
    dest = models.file_dest(spec, spec.files[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"0" * 3)  # spec says size_bytes=10

    assert models.is_downloaded(spec) is False


def test_downloading_status_wins_regardless_of_disk_state():
    spec = _spec()
    assert models.status(spec, loaded=False, downloading=True) == models.ModelStatus.DOWNLOADING


def test_same_filename_across_files_does_not_collide(tmp_path, monkeypatch):
    # OCR detector and recognizer both ship a file literally named
    # inference.onnx from different HF repos -- file_dest must not collide.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    spec = _spec(n_files=2)
    dest0 = models.file_dest(spec, spec.files[0])
    dest1 = models.file_dest(spec, spec.files[1])
    assert dest0 != dest1


def test_delete_files_removes_downloaded_weights(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    spec = _spec()
    dest = models.file_dest(spec, spec.files[0])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"0" * 10)

    models.delete_files(spec)

    assert not dest.exists()
    assert models.is_downloaded(spec) is False


HUMAN_SIZE_CASES = [
    (0, "0 B"),
    (500, "500 B"),
    (1024, "1.0 KB"),
    (98_247_878, "93.7 MB"),
]


def test_human_size():
    for num_bytes, expected in HUMAN_SIZE_CASES:
        assert models.human_size(num_bytes) == expected
