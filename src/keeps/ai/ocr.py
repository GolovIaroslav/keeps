"""OCR: PP-OCRv5 two-stage pipeline (detector + East Slavic recognizer), CPU ONNX
Runtime (PLAN.md §9). cv2/onnxruntime/numpy are imported lazily inside functions,
not at module level.

Preprocess/postprocess parameters below are taken verbatim from the two models'
own inference.yml (verified live against the Hugging Face repos on 2026-07-10,
see PLAN.md §9) -- not guessed, not copied from an unrelated PP-OCR version.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DICT_PATH = Path(__file__).parent / "data" / "eslav_dict.txt"

# Detector (PaddlePaddle/PP-OCRv5_mobile_det_onnx, inference.yml).
DET_RESIZE_LONG = 960
DET_MEAN = (0.485, 0.456, 0.406)
DET_STD = (0.229, 0.224, 0.225)
DET_THRESH = 0.3
DET_BOX_THRESH = 0.6
DET_UNCLIP_RATIO = 1.5
DET_MIN_BOX_AREA = 16

# Recognizer (PaddlePaddle/eslav_PP-OCRv5_mobile_rec_onnx, inference.yml).
REC_HEIGHT = 48
REC_BLANK_INDEX = 0


@lru_cache(maxsize=1)
def load_char_list() -> tuple[str, ...]:
    """blank (index 0) + the 517-entry East Slavic dict + a trailing space,
    matching PaddleOCR's standard CTCLabelDecode convention (`use_space_char`).
    """
    chars = DICT_PATH.read_text(encoding="utf-8").splitlines()
    return ("<blank>", *chars, " ")


def ctc_collapse(class_ids: list[int], blank_index: int = REC_BLANK_INDEX) -> list[int]:
    """CTC greedy decode step 1: drop consecutive duplicates, then drop blanks."""
    collapsed = []
    previous = None
    for class_id in class_ids:
        if class_id != previous:
            collapsed.append(class_id)
        previous = class_id
    return [class_id for class_id in collapsed if class_id != blank_index]


def decode_indices(indices: list[int], char_list: tuple[str, ...]) -> str:
    return "".join(char_list[i] for i in indices)


def ctc_greedy_decode(class_ids: list[int], char_list: tuple[str, ...]) -> str:
    return decode_indices(ctc_collapse(class_ids), char_list)


def _resize_for_detection(height: int, width: int, max_side: int = DET_RESIZE_LONG):
    scale = max_side / max(height, width)
    new_h = max(32, round(height * scale / 32) * 32)
    new_w = max(32, round(width * scale / 32) * 32)
    return new_h, new_w


def _preprocess_detection(image_rgb):
    import numpy as np

    height, width = image_rgb.shape[:2]
    new_h, new_w = _resize_for_detection(height, width)

    import cv2

    resized = cv2.resize(image_rgb, (new_w, new_h))
    normalized = resized.astype(np.float32) / 255.0
    normalized = (normalized - np.array(DET_MEAN, dtype=np.float32)) / np.array(
        DET_STD, dtype=np.float32
    )
    tensor = np.expand_dims(np.transpose(normalized, (2, 0, 1)), axis=0)
    return tensor, (height, width), (new_h, new_w)


def unclip_box(points, ratio: float = DET_UNCLIP_RATIO):
    """Expand a 4-point polygon outward along its edge normals (DBNet unclip).

    `points` is a (4, 2) array-like of (x, y). Offset distance follows the
    DBPostProcess formula: D = area * ratio / perimeter, applied per-edge.
    """
    import cv2
    import numpy as np

    points = np.asarray(points, dtype=np.float32)
    area = cv2.contourArea(points)
    perimeter = cv2.arcLength(points, True)
    if perimeter == 0 or area == 0:
        return points
    distance = area * ratio / perimeter

    # The outward normal's sign depends on the polygon's winding order, which
    # findContours/minAreaRect don't guarantee is fixed across all inputs --
    # derive it from the signed shoelace sum instead of assuming one winding,
    # or this can silently shrink the box instead of expanding it.
    n = len(points)
    signed_area = sum(
        points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
        for i in range(n)
    )
    sign = 1.0 if signed_area > 0 else -1.0

    offset = np.zeros_like(points)
    for i in range(n):
        p1, p2 = points[i], points[(i + 1) % n]
        edge = p2 - p1
        edge_len = np.linalg.norm(edge)
        if edge_len == 0:
            continue
        normal = sign * np.array([edge[1], -edge[0]], dtype=np.float32) / edge_len
        offset[i] += normal * distance
        offset[(i + 1) % n] += normal * distance
    return points + offset


def order_points_clockwise(points):
    """Reorder 4 arbitrary quad points to [top-left, top-right, bottom-right,
    bottom-left]. `cv2.boxPoints` returns points in an order that shifts with
    the rectangle's rotation angle, not a fixed corner -- feeding them
    unordered into `cv2.getPerspectiveTransform` warps the crop into a
    rotated/mirrored image instead of an upright one (PaddleOCR's own
    `get_mini_boxes` does the same reorder before cropping).
    """
    import numpy as np

    pts = sorted(points, key=lambda p: p[0])
    left, right = sorted(pts[:2], key=lambda p: p[1]), sorted(pts[2:], key=lambda p: p[1])
    top_left, bottom_left = left
    top_right, bottom_right = right
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype=np.float32)


def _postprocess_detection(prob_map, orig_shape, resized_shape):
    import cv2
    import numpy as np

    orig_h, orig_w = orig_shape
    res_h, res_w = resized_shape
    prob_map = prob_map[0, 0]
    binary = (prob_map > DET_THRESH).astype(np.uint8) * 255
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    boxes = []
    for contour in contours:
        if cv2.contourArea(contour) < DET_MIN_BOX_AREA:
            continue
        rect = cv2.minAreaRect(contour)
        box = order_points_clockwise(cv2.boxPoints(rect))

        mask = np.zeros(prob_map.shape, dtype=np.uint8)
        cv2.fillPoly(mask, [box.astype(np.int32)], 1)
        if mask.sum() == 0 or prob_map[mask.astype(bool)].mean() < DET_BOX_THRESH:
            continue

        box = unclip_box(box)
        box[:, 0] = np.clip(box[:, 0] * (orig_w / res_w), 0, orig_w - 1)
        box[:, 1] = np.clip(box[:, 1] * (orig_h / res_h), 0, orig_h - 1)
        boxes.append(box.astype(np.int32))

    return sorted(boxes, key=lambda b: b[:, 1].mean())  # reading order: top to bottom


def _preprocess_recognition_crop(image_rgb, box):
    import cv2
    import numpy as np

    box = box.astype(np.float32)
    width = int(
        max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))
    )
    height = int(
        max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))
    )
    width, height = max(width, 1), max(height, 1)

    dst = np.array(
        [[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(box, dst)
    warped = cv2.warpPerspective(image_rgb, matrix, (width, height))

    target_w = max(16, round((REC_HEIGHT * width / height) / 8) * 8)
    resized = cv2.resize(warped, (target_w, REC_HEIGHT))
    normalized = (resized.astype(np.float32) / 255.0 - 0.5) / 0.5
    return np.expand_dims(np.transpose(normalized, (2, 0, 1)), axis=0)


class OcrEngine:
    """Lazy wrapper around the detector + recognizer ONNX sessions."""

    def __init__(self, det_path: Path, rec_path: Path) -> None:
        self._det_path = det_path
        self._rec_path = rec_path
        self._det_session = None
        self._rec_session = None

    @property
    def is_loaded(self) -> bool:
        return self._det_session is not None

    def load(self) -> None:
        if self._det_session is not None:
            return
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        providers = ["CPUExecutionProvider"]
        self._det_session = ort.InferenceSession(
            str(self._det_path), sess_options=options, providers=providers
        )
        self._rec_session = ort.InferenceSession(
            str(self._rec_path), sess_options=options, providers=providers
        )

    def unload(self) -> None:
        self._det_session = None
        self._rec_session = None

    def extract_text(self, png_bytes: bytes) -> str:
        import cv2
        import numpy as np

        self.load()
        image_bgr = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image_bgr is None:
            return ""
        image_rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)

        det_tensor, orig_shape, resized_shape = _preprocess_detection(image_rgb)
        det_input_name = self._det_session.get_inputs()[0].name
        (prob_map,) = self._det_session.run(None, {det_input_name: det_tensor})
        boxes = _postprocess_detection(prob_map, orig_shape, resized_shape)

        char_list = load_char_list()
        rec_input_name = self._rec_session.get_inputs()[0].name
        lines = []
        for box in boxes:
            rec_tensor = _preprocess_recognition_crop(image_rgb, box)
            (logits,) = self._rec_session.run(None, {rec_input_name: rec_tensor})
            class_ids = np.argmax(logits[0], axis=-1).tolist()
            text = ctc_greedy_decode(class_ids, char_list)
            if text.strip():
                lines.append(text)
        return "\n".join(lines)
