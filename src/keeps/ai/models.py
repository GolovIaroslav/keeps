"""Registry of downloadable AI model weights: specs, disk paths, status (PLAN.md §9.1).

Pure logic, no Qt, no network -- ai/download.py does the actual fetching.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ModelStatus(Enum):
    NOT_DOWNLOADED = "not_downloaded"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"  # on disk, no live session in RAM
    LOADED = "loaded"  # on disk and a live session/object is held in RAM


@dataclass(frozen=True)
class ModelFile:
    """One file that must exist on disk for its model to count as downloaded."""

    repo: str
    path_in_repo: str
    sha256: str
    size_bytes: int

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.path_in_repo}"

    @property
    def filename(self) -> str:
        return self.path_in_repo.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class ModelSpec:
    """A named model made of one or more files sharing one on-disk directory."""

    name: str
    label: str
    files: tuple[ModelFile, ...]

    @property
    def total_size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


def models_dir() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "keeps" / "models"


def model_dir(spec: ModelSpec) -> Path:
    return models_dir() / spec.name


def file_dest(spec: ModelSpec, file: ModelFile) -> Path:
    # Subdirectory named after the repo's last path segment: a spec can bundle
    # multiple files that all happen to be named "inference.onnx" (OCR
    # detector + recognizer each ship one under that same filename upstream).
    repo_slug = file.repo.rsplit("/", 1)[-1]
    return model_dir(spec) / repo_slug / file.filename


def is_downloaded(spec: ModelSpec) -> bool:
    """All files present with the expected size (cheap check, no re-hashing)."""
    return all(
        file_dest(spec, f).is_file() and file_dest(spec, f).stat().st_size == f.size_bytes
        for f in spec.files
    )


def status(spec: ModelSpec, loaded: bool, downloading: bool = False) -> ModelStatus:
    if downloading:
        return ModelStatus.DOWNLOADING
    if not is_downloaded(spec):
        return ModelStatus.NOT_DOWNLOADED
    return ModelStatus.LOADED if loaded else ModelStatus.DOWNLOADED


def delete_files(spec: ModelSpec) -> None:
    for f in spec.files:
        dest = file_dest(spec, f)
        if dest.is_file():
            dest.unlink()
        try:
            dest.parent.rmdir()
        except OSError:
            pass  # not empty (rare: sibling file), or already gone


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# -- Registry (verified live against Hugging Face's file listing + LFS oid
# sha256 on 2026-07-10, see PLAN.md §9) --------------------------------------

TEXT_EMBED = ModelSpec(
    name="text-embed-granite-97m",
    label="Text embeddings (Granite 97M Multilingual R2)",
    files=(
        ModelFile(
            repo="ibm-granite/granite-embedding-97m-multilingual-r2",
            path_in_repo="onnx/model_quint8_avx2.onnx",
            sha256="a6022dd8220ea6f6595562a1328ee216f4a94faa55362f2f4747c80f1e78772e",
            size_bytes=98_247_878,
        ),
        ModelFile(
            repo="ibm-granite/granite-embedding-97m-multilingual-r2",
            path_in_repo="tokenizer.json",
            sha256="4f2842d568e2724370aec203652a42ac783c7937f8347a1a2cc7506d71f1582f",
            size_bytes=25_301_672,
        ),
    ),
)

OCR = ModelSpec(
    name="ocr-ppocrv5-eslav",
    label="OCR (PP-OCRv5, East Slavic RU/UK/BE/BG + EN)",
    files=(
        ModelFile(
            repo="PaddlePaddle/PP-OCRv5_mobile_det_onnx",
            path_in_repo="inference.onnx",
            sha256="a431985659dc921974177a95adcfbb90fd9e51989a5e04d70d0b75f597b6e61d",
            size_bytes=4_826_518,
        ),
        ModelFile(
            repo="PaddlePaddle/eslav_PP-OCRv5_mobile_rec_onnx",
            path_in_repo="inference.onnx",
            sha256="b3018ef2b09a0250b6e0c8e871c927098363e5fd4df890cc68e8358eb0aaf1bd",
            size_bytes=7_887_627,
        ),
    ),
)
