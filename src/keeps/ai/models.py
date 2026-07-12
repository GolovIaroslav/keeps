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

OCR_DET = ModelSpec(
    name="ocr-det",
    label="OCR text detector (shared by all languages)",
    files=(
        ModelFile(
            repo="PaddlePaddle/PP-OCRv5_mobile_det_onnx",
            path_in_repo="inference.onnx",
            sha256="a431985659dc921974177a95adcfbb90fd9e51989a5e04d70d0b75f597b6e61d",
            size_bytes=4_826_518,
        ),
    ),
)

# One PP-OCRv5 recognizer per language group (verified live against Hugging
# Face on 2026-07-12): each has its own ONNX weights AND its own CTC
# character dictionary (src/keeps/ai/data/<code>_dict.txt) -- they are not
# interchangeable. All share the OCR_DET detector above and the same
# [3, 48, 320] input shape. Which of these are active is fully up to the
# user (Settings > AI > OCR languages, ai/ocr_languages, Ф9.6) -- "eslav"
# alone is only the shipped default, preserving pre-Ф9.6 behavior.
OCR_REC: dict[str, ModelSpec] = {
    "ch": ModelSpec(
        name="ocr-rec-ch",
        label="OCR recognizer: Chinese, English, Japanese",
        files=(
            ModelFile(
                repo="PaddlePaddle/PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="da72dc72ca4dc220df0dfde68c1dedc31c58d3e76a25871122e5056227d50092",
                size_bytes=16_534_782,
            ),
        ),
    ),
    "en": ModelSpec(
        name="ocr-rec-en",
        label="OCR recognizer: English (optimized)",
        files=(
            ModelFile(
                repo="PaddlePaddle/en_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="b5f833dfc5d0eb71da397b4efa06ebeee9b431b690a47d6af40d77d8eabc557f",
                size_bytes=7_848_423,
            ),
        ),
    ),
    "eslav": ModelSpec(
        name="ocr-rec-eslav",
        label="OCR recognizer: East Slavic (Russian, Belarusian, Ukrainian) + English",
        files=(
            ModelFile(
                repo="PaddlePaddle/eslav_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="b3018ef2b09a0250b6e0c8e871c927098363e5fd4df890cc68e8358eb0aaf1bd",
                size_bytes=7_887_627,
            ),
        ),
    ),
    "cyrillic": ModelSpec(
        name="ocr-rec-cyrillic",
        label="OCR recognizer: Cyrillic-script languages (RU, UK, BG, KK, and ~10 more) + EN",
        files=(
            ModelFile(
                repo="PaddlePaddle/cyrillic_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="5371ee1ddaa7983cc62d0818d99e982b6804638c85e4f960d59a574094e172e5",
                size_bytes=8_048_799,
            ),
        ),
    ),
    "latin": ModelSpec(
        name="ocr-rec-latin",
        label="OCR recognizer: Latin-script languages (FR, DE, ES, IT, PL, and ~40 more) + EN",
        files=(
            ModelFile(
                repo="PaddlePaddle/latin_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="7888113072263cb471b93f66dd5e2ad70548dc526fa1ace760d0d973dd121498",
                size_bytes=8_042_023,
            ),
        ),
    ),
    "korean": ModelSpec(
        name="ocr-rec-korean",
        label="OCR recognizer: Korean + English",
        files=(
            ModelFile(
                repo="PaddlePaddle/korean_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="92f0b7785e64fc9090106a241cf4c1eb97472824558272751b88a2a4476d3a08",
                size_bytes=13_418_787,
            ),
        ),
    ),
    "th": ModelSpec(
        name="ocr-rec-th",
        label="OCR recognizer: Thai + English",
        files=(
            ModelFile(
                repo="PaddlePaddle/th_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="27618be66018f8598ac0a526a593f9f1cebf794e7eded93428e8fb016e537f5f",
                size_bytes=7_891_015,
            ),
        ),
    ),
    "el": ModelSpec(
        name="ocr-rec-el",
        label="OCR recognizer: Greek + English",
        files=(
            ModelFile(
                repo="PaddlePaddle/el_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="2acf17fcaea2bc81b878e311e6263b8885f48bb03796f75f9f30ed3242bbaa6d",
                size_bytes=7_808_735,
            ),
        ),
    ),
    "arabic": ModelSpec(
        name="ocr-rec-arabic",
        label="OCR recognizer: Arabic, Persian, Urdu, and 6 more Arabic-script languages + EN",
        files=(
            ModelFile(
                repo="PaddlePaddle/arabic_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="799113ebf267fbe742deb99eb36e8d42c9ddc5291ceacf92add41b4d52a59110",
                size_bytes=7_998_947,
            ),
        ),
    ),
    "devanagari": ModelSpec(
        name="ocr-rec-devanagari",
        label="OCR recognizer: Hindi, Marathi, Nepali, Sanskrit, and related Indic scripts + EN",
        files=(
            ModelFile(
                repo="PaddlePaddle/devanagari_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="cb789212ce96c69d3e74728ae4309d179281d68cb3945d0616b67cafab41c986",
                size_bytes=7_912_311,
            ),
        ),
    ),
    "ta": ModelSpec(
        name="ocr-rec-ta",
        label="OCR recognizer: Tamil + English",
        files=(
            ModelFile(
                repo="PaddlePaddle/ta_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="c6d2b682d2a0ea4cb1fccdba295976f93fd439964d16cdc666cadef531accbee",
                size_bytes=7_885_691,
            ),
        ),
    ),
    "te": ModelSpec(
        name="ocr-rec-te",
        label="OCR recognizer: Telugu + English",
        files=(
            ModelFile(
                repo="PaddlePaddle/te_PP-OCRv5_mobile_rec_onnx",
                path_in_repo="inference.onnx",
                sha256="8238bfc46d4cffe720ed6706e3842802467343497428693ff2bfb4e6b3caa36b",
                size_bytes=7_898_759,
            ),
        ),
    ),
}
