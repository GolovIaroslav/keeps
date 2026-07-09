"""Opt-in AI search: text embeddings + OCR (PLAN.md §9). Heavy imports (onnxruntime,
tokenizers, cv2, numpy) live inside the functions/classes that need them, never at
module level -- importing this package must stay free even when all ai/* toggles
are off.
"""
