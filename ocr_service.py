# ==========================
# OCR SERVICE  (FREE / LOCAL — TrOCR)
# ==========================
#
# Handles image preprocessing and handwriting OCR for the Bulk Handwritten
# Collection OCR feature.
#
# PROVIDER: Microsoft TrOCR (microsoft/trocr-base-handwritten), run locally
# via Hugging Face `transformers`. No API key, no per-request cost, no
# internet call at inference time (only the first run downloads the model
# weights once, then they're cached on disk forever).
#
# TrOCR reads a cropped piece of handwriting; it has no built-in idea
# of "which part of this page is a real entry". This module therefore
# detects vertical collection-sheet columns first, then individual rows
# inside each column, runs TrOCR on each row crop, and parses the text into
# {customer_reg_no, amount}.
#
# This is genuinely free forever, but the tradeoff vs a cloud vision model
# is accuracy on messy pages (crossed-out entries, stray marks, uneven
# rows) - the line-segmentation + regex-parsing step below is heuristic
# and will need tuning against real handwriting samples from your sheets.
#
# To switch providers later: implement a new `_ocr_lines(...)`-shaped
# function and point `extract_entries_from_image()` at it. Nothing in
# app.py or the template needs to change - only this file.

import io
import logging
import os
import re

import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

OCR_PROVIDER = os.getenv("OCR_PROVIDER", "trocr").strip().lower()
TROCR_MODEL_NAME = os.getenv("TROCR_MODEL_NAME", "microsoft/trocr-base-handwritten")

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
MAX_IMAGE_SIDE = 2200  # px
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB per image, pre-processing

# Line-segmentation tuning knobs (see _segment_lines below).
MIN_LINE_HEIGHT_PX = 18       # ignore bands of ink thinner than this
LINE_GAP_MERGE_PX = 6         # merge bands separated by a gap smaller than this
LINE_PADDING_PX = 6           # padding added around each detected line crop

# A "confidence" below this is flagged for manual review by the app.
LOW_CONFIDENCE_THRESHOLD = 80


class OCRServiceError(Exception):
    """Raised for any OCR failure the route should turn into a friendly
    error message (never expose the raw exception/stack trace to the user)."""


# ---------------------------------------------------------------------
# IMAGE PREPROCESSING
# ---------------------------------------------------------------------

def preprocess_image(raw_bytes, rotation=0):
    """
    Preprocess a photographed collection sheet, WITHOUT mutating the
    caller's original bytes.

    Steps: EXIF-orientation correction -> downscale if huge -> grayscale
    -> contrast enhancement.

    Returns: a PIL.Image in "L" (grayscale) mode.
    """
    try:
        img = Image.open(io.BytesIO(raw_bytes))
    except Exception as exc:
        raise OCRServiceError("That file doesn't look like a valid image.") from exc

    try:
        img = ImageOps.exif_transpose(img)
        if rotation in (90, 180, 270):
            # PIL positive angles rotate counter-clockwise; expand keeps
            # the whole photographed page visible.
            img = img.rotate(rotation, expand=True)
        img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
        img = img.convert("L")
        img = ImageOps.autocontrast(img, cutoff=1)
        return img
    except Exception as exc:
        raise OCRServiceError("Could not process this image.") from exc


def validate_upload(file_storage):
    """Validate a single uploaded file before any processing happens.
    Raises OCRServiceError with a user-safe message on failure."""
    if file_storage is None or file_storage.filename == "":
        raise OCRServiceError("One of the uploaded files was empty.")

    mimetype = (file_storage.mimetype or "").lower()
    if mimetype not in ALLOWED_IMAGE_TYPES:
        raise OCRServiceError(
            f"Unsupported file type: {file_storage.filename}. "
            "Please upload JPG, PNG, or WEBP images only."
        )

    file_storage.stream.seek(0, os.SEEK_END)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size == 0:
        raise OCRServiceError(f"{file_storage.filename} is an empty file.")
    if size > MAX_IMAGE_BYTES:
        raise OCRServiceError(f"{file_storage.filename} is too large (max 10 MB per image).")

    return file_storage.read()


# ---------------------------------------------------------------------
# COLUMN + ROW SEGMENTATION
# ---------------------------------------------------------------------
#
# IMPORTANT:
# The previous implementation segmented the entire page horizontally.
# That works for a one-column note, but fails on collection sheets such as:
#
#   7841 100 | 7861 100 | 7881 100 | 7901 100
#
# The OCR model would receive several customers in one crop. We now split
# vertical columns FIRST and then split rows INSIDE each column.

MIN_COLUMN_WIDTH_PX = 120
MAX_COLUMNS = 8

# A ruling line is usually long and continuous. Handwritten digits are not.
# 8% is deliberately lower than the previous 15% because photographed
# sheets can have a short/broken vertical separator.
VERTICAL_LINE_MIN_RUN_RATIO = 0.08
COLUMN_PADDING_PX = 12

MIN_LINE_HEIGHT_PX = 12
LINE_GAP_MERGE_PX = 4
LINE_PADDING_PX = 6
ROW_INK_RATIO = 0.003

# Ignore the very top/bottom of a page. This prevents titles, page headings,
# and bottom TOTAL boxes from becoming false collection entries.
TOP_IGNORE_RATIO = 0.08
BOTTOM_IGNORE_RATIO = 0.92


def _binary_ink(gray_img):
    """Return (grayscale array, binary dark-ink mask)."""
    arr = np.array(gray_img)

    # A fixed upper cap avoids treating photograph shadows/background as ink.
    # The percentile still adapts to darker/lighter photographs.
    threshold = min(180, np.percentile(arr, 45))
    return arr, (arr < threshold).astype(np.uint8)


def _group_true_runs(values):
    """Group consecutive True values into inclusive (start, end) ranges."""
    groups = []
    start = None

    for i, value in enumerate(values):
        if value and start is None:
            start = i
        elif not value and start is not None:
            groups.append((start, i - 1))
            start = None

    if start is not None:
        groups.append((start, len(values) - 1))

    return groups


def _longest_vertical_runs(binary):
    """Return the longest continuous dark-pixel run for every image column."""
    height, width = binary.shape
    longest = np.zeros(width, dtype=np.int32)

    for x in range(width):
        col = binary[:, x]
        padded = np.r_[0, col, 0]

        starts = np.flatnonzero(
            (padded[1:-1] == 1) & (padded[:-2] == 0)
        )
        ends = np.flatnonzero(
            (padded[1:-1] == 1) & (padded[2:] == 0)
        )

        if len(starts) and len(ends):
            longest[x] = int(np.max(ends - starts + 1))

    return longest


def _segment_columns(gray_img):
    """
    Detect long vertical ruling lines and split the page into columns.

    Returns columns from left to right. If no reliable vertical separators
    are found, the whole page is returned as one column so single-column
    sheets continue to work.
    """
    _, binary = _binary_ink(gray_img)
    height, width = binary.shape

    if width < MIN_COLUMN_WIDTH_PX * 2:
        return [gray_img]

    min_run = max(20, int(height * VERTICAL_LINE_MIN_RUN_RATIO))
    vertical_runs = _longest_vertical_runs(binary)

    separator_mask = vertical_runs >= min_run
    separator_groups = _group_true_runs(separator_mask)

    separators = []

    for x0, x1 in separator_groups:
        center = (x0 + x1) // 2

        # Ignore page-edge lines/borders.
        if center < width * 0.04 or center > width * 0.96:
            continue

        # Ignore very wide regions; they are more likely a dark object,
        # page border, or photograph artifact than a column rule.
        if x1 - x0 + 1 > max(20, int(width * 0.03)):
            continue

        separators.append(center)

    # Need at least one separator to form multiple columns.
    if not separators or len(separators) >= MAX_COLUMNS:
        return [gray_img]

    boundaries = [0] + separators + [width]
    columns = []

    for left, right in zip(boundaries, boundaries[1:]):
        x0 = left + (COLUMN_PADDING_PX if left else 0)
        x1 = right - (COLUMN_PADDING_PX if right < width else 0)

        if x1 - x0 >= MIN_COLUMN_WIDTH_PX:
            columns.append(gray_img.crop((x0, 0, x1, height)))

    if len(columns) < 2:
        return [gray_img]

    logger.info("OCR segmentation detected %d column(s).", len(columns))
    return columns


def _segment_lines(gray_img):
    """
    Split ONE column into individual handwritten row crops.

    Header underlines are too thin to pass MIN_LINE_HEIGHT_PX. Page titles
    and bottom TOTAL boxes are excluded by the top/bottom page margins.
    """
    _, binary = _binary_ink(gray_img)
    height, width = binary.shape

    row_ink = binary.sum(axis=1)
    is_ink_row = row_ink > max(2, int(width * ROW_INK_RATIO))

    bands = []
    start = None
    gap = 0

    for y, has_ink in enumerate(is_ink_row):
        if has_ink:
            if start is None:
                start = y
            gap = 0
        elif start is not None:
            gap += 1
            if gap > LINE_GAP_MERGE_PX:
                bands.append((start, y - gap))
                start = None
                gap = 0

    if start is not None:
        bands.append((start, len(is_ink_row) - 1))

    crops = []
    top_limit = int(height * TOP_IGNORE_RATIO)
    bottom_limit = int(height * BOTTOM_IGNORE_RATIO)

    for y0, y1 in bands:
        if (y1 - y0 + 1) < MIN_LINE_HEIGHT_PX:
            continue

        # Remove page title/header area and bottom total/signature area.
        if y1 < top_limit:
            continue
        if y0 > bottom_limit:
            continue

        top = max(0, y0 - LINE_PADDING_PX)
        bottom = min(height, y1 + 1 + LINE_PADDING_PX)

        # Keep the full column width so Reg No and Amount stay together.
        crops.append(gray_img.crop((0, top, width, bottom)))

    return crops


# ---------------------------------------------------------------------
# TrOCR MODEL  (lazy-loaded singleton - loaded once per process)
# ---------------------------------------------------------------------

_trocr_processor = None
_trocr_model = None


def _load_trocr():
    global _trocr_processor, _trocr_model
    if _trocr_model is not None:
        return _trocr_processor, _trocr_model

    try:
        import torch  # noqa: F401  (imported to fail fast with a clear error if missing)
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
    except ImportError as exc:
        raise OCRServiceError(
            "OCR is not configured on the server "
            "(missing 'transformers'/'torch' packages)."
        ) from exc

    logger.info("Loading TrOCR model '%s' (first run downloads it once)...", TROCR_MODEL_NAME)
    _trocr_processor = TrOCRProcessor.from_pretrained(TROCR_MODEL_NAME)
    _trocr_model = VisionEncoderDecoderModel.from_pretrained(TROCR_MODEL_NAME)
    _trocr_model.eval()
    return _trocr_processor, _trocr_model


def _confidence_from_scores(scores, batch_index, batch_size):
    """
    Convert TrOCR token scores into a conservative 0-100 confidence proxy.
    This is not a calibrated probability.
    """
    import torch

    if not scores:
        return 0

    probs = []
    for step_logits in scores:
        # With greedy generation the first dimension is batch_size.
        if step_logits.shape[0] < batch_size:
            continue
        step_probs = torch.softmax(step_logits[batch_index], dim=-1)
        probs.append(float(step_probs.max()))

    if not probs:
        return 0

    # Geometric mean penalizes one very uncertain token more than an
    # arithmetic mean does.
    log_sum = sum(np.log(max(p, 1e-8)) for p in probs)
    geometric = float(np.exp(log_sum / len(probs)))
    return int(round(max(0.0, min(1.0, geometric)) * 100))


def _ocr_images(images, max_new_tokens=16, batch_size=8):
    """
    OCR a list of PIL images in batches. Batching substantially reduces
    Python/model overhead for 40-80+ entry collection sheets.
    Returns [(text, confidence), ...] in the same order as `images`.
    """
    if not images:
        return []

    import torch

    processor, model = _load_trocr()
    results = []

    # CPU inference is usually faster and more predictable when gradients
    # and autograd bookkeeping are completely disabled.
    with torch.inference_mode():
        for start in range(0, len(images), max(1, batch_size)):
            batch = images[start:start + batch_size]
            rgb_batch = [img.convert("RGB") for img in batch]

            pixel_values = processor(
                images=rgb_batch,
                return_tensors="pt",
                padding=True,
            ).pixel_values

            outputs = model.generate(
                pixel_values,
                output_scores=True,
                return_dict_in_generate=True,
                max_new_tokens=max_new_tokens,
                num_beams=1,
                do_sample=False,
            )

            texts = processor.batch_decode(
                outputs.sequences,
                skip_special_tokens=True,
            )

            for i, raw_text in enumerate(texts):
                text = raw_text.strip()
                confidence = _confidence_from_scores(
                    outputs.scores,
                    i,
                    len(batch),
                )
                results.append((text, confidence))

    return results


def _ocr_line(line_img):
    """Backward-compatible single-image wrapper around the batched OCR."""
    result = _ocr_images([line_img], max_new_tokens=20, batch_size=1)
    return result[0] if result else ("", 0)


# ---------------------------------------------------------------------
# TEXT -> {customer_reg_no, amount} PARSING
# ---------------------------------------------------------------------

# Common handwriting/OCR confusions for numeric-only fields.
_DIGIT_FIXES = str.maketrans({
    "O": "0", "o": "0", "D": "0",
    "I": "1", "l": "1", "|": "1",
    "Z": "2", "z": "2",
    "S": "5", "s": "5",
    "G": "6", "g": "6",
    "T": "7", "t": "7",
    "B": "8", "b": "8",
    "q": "9",
})


def _digits_only(text):
    """Normalize common OCR confusions and return the digit sequence."""
    if not text:
        return ""

    normalized = text.translate(_DIGIT_FIXES)
    return "".join(ch for ch in normalized if ch.isdigit())


def _parse_numeric_field(text, field):
    """
    Parse a numeric-only OCR field.

    `field` is either "reg_no" or "amount". Keeping these separate prevents
    the old regex from accidentally combining digits from neighboring fields.
    """
    digits = _digits_only(text)

    if field == "reg_no":
        return digits if 2 <= len(digits) <= 10 else ""

    if field == "amount":
        return digits if 1 <= len(digits) <= 7 else ""

    return ""


def _parse_line(text):
    """
    Backward-compatible parser for a complete row containing:
        registration number + amount
    """
    if not text:
        return None

    digits = _digits_only(text)

    # Prefer the common customer-id shape of 4+ digits followed by a
    # shorter amount. This prevents a title/total from becoming a payment.
    match = re.search(r"(\d{4,10})\D{0,8}(\d{1,7})", text)
    if match:
        reg_no = _parse_numeric_field(match.group(1), "reg_no")
        amount_str = _parse_numeric_field(match.group(2), "amount")
    else:
        # Fallback for OCR that removed the separator completely.
        match_digits = re.search(r"(\d{4,10})(\d{1,7})$", digits)
        if not match_digits:
            return None
        reg_no = match_digits.group(1)
        amount_str = match_digits.group(2)

    if not reg_no or not amount_str:
        return None

    try:
        amount = float(amount_str)
    except ValueError:
        return None

    if amount <= 0:
        return None

    return reg_no, amount


# ---------------------------------------------------------------------
# ENTRY FIELD CROPPING
# ---------------------------------------------------------------------

def _crop_entry_fields(row_img):
    """
    Split one row into two overlapping fields:
      left  = customer registration number
      right = amount

    The overlap gives TrOCR enough whitespace/context without allowing a
    neighboring column to enter the crop.
    """
    width, height = row_img.size

    # Collection sheets normally place Reg No on the left and Amount on the
    # right. Slight overlap handles uneven handwriting placement.
    split = int(width * 0.57)
    overlap = max(8, int(width * 0.05))

    reg = row_img.crop((0, 0, min(width, split + overlap), height))
    amount = row_img.crop((max(0, split - overlap), 0, width, height))

    return reg, amount


def _looks_like_entry(reg_no, amount_str):
    """
    Conservative sanity check before adding an OCR result.
    Database validation still happens later in app.py.
    """
    if not (2 <= len(reg_no) <= 10):
        return False
    if not amount_str:
        return False

    try:
        amount = float(amount_str)
    except ValueError:
        return False

    # Prevent obvious OCR garbage while allowing normal collection values.
    return 0 < amount <= 10_000_000


def extract_entries_from_image(raw_bytes, rotation=0):
    """
    Bulk-collection OCR pipeline:

        preprocess
        -> detect columns
        -> detect rows inside each column
        -> crop Reg No + Amount fields separately
        -> batched TrOCR
        -> numeric normalization
        -> conservative parsing

    The result contains one record per successfully recognized entry.
    Unreadable/header/total rows are ignored instead of becoming false
    payments. Customer existence and duplicate validation remain the
    responsibility of app.py.
    """
    if OCR_PROVIDER != "trocr":
        raise OCRServiceError(f"Unknown OCR_PROVIDER configured: {OCR_PROVIDER}")

    gray_img = preprocess_image(raw_bytes, rotation=rotation)

    try:
        columns = _segment_columns(gray_img)
    except Exception as exc:
        logger.exception("Column segmentation failed")
        raise OCRServiceError("Could not detect columns in this image.") from exc

    # Build all row crops first. This lets us OCR in batches, which is much
    # faster for large sheets than calling model.generate() once per row.
    row_items = []

    for column_index, column_img in enumerate(columns, start=1):
        try:
            line_crops = _segment_lines(column_img)
        except Exception:
            logger.exception(
                "Row segmentation failed in column %d", column_index
            )
            continue

        logger.info(
            "OCR column %d/%d: detected %d row crop(s).",
            column_index,
            len(columns),
            len(line_crops),
        )

        for row_index, row_img in enumerate(line_crops, start=1):
            reg_img, amount_img = _crop_entry_fields(row_img)
            row_items.append(
                (column_index, row_index, row_img, reg_img, amount_img)
            )

    if not row_items:
        return []

    # OCR Reg No and Amount independently. This is the major accuracy
    # improvement: digits from neighboring fields cannot be joined by regex.
    reg_results = _ocr_images(
        [item[3] for item in row_items],
        max_new_tokens=12,
        batch_size=8,
    )
    amount_results = _ocr_images(
        [item[4] for item in row_items],
        max_new_tokens=10,
        batch_size=8,
    )

    entries = []

    for item, (reg_text, reg_conf), (amount_text, amount_conf) in zip(
        row_items, reg_results, amount_results
    ):
        column_index, row_index, row_img, _, _ = item

        reg_no = _parse_numeric_field(reg_text, "reg_no")
        amount_str = _parse_numeric_field(amount_text, "amount")

        # If field OCR failed, fall back to OCR'ing the complete row once.
        # This helps sheets where the handwritten fields are not aligned
        # with the normal 57/43% layout.
        fallback_text = ""
        fallback_conf = 0

        if not reg_no or not amount_str:
            try:
                fallback_text, fallback_conf = _ocr_line(row_img)
                parsed = _parse_line(fallback_text)
                if parsed:
                    fallback_reg, fallback_amount = parsed
                    if not reg_no:
                        reg_no = fallback_reg
                    if not amount_str:
                        amount_str = str(int(fallback_amount))
            except OCRServiceError:
                raise
            except Exception:
                logger.exception(
                    "Fallback OCR failed on column %d row %d",
                    column_index,
                    row_index,
                )

        if not _looks_like_entry(reg_no, amount_str):
            continue

        try:
            amount = float(amount_str)
        except ValueError:
            continue

        confidence = min(reg_conf, amount_conf)
        if fallback_conf and confidence == 0:
            confidence = fallback_conf

        notes_parts = []

        if not reg_no or not amount_str:
            notes_parts.append("partial OCR")

        if reg_conf < LOW_CONFIDENCE_THRESHOLD:
            notes_parts.append(f"reg read: '{reg_text}'")

        if amount_conf < LOW_CONFIDENCE_THRESHOLD:
            notes_parts.append(f"amount read: '{amount_text}'")

        if fallback_text:
            notes_parts.append(f"row read: '{fallback_text}'")

        entries.append({
            "customer_reg_no": reg_no,
            "amount": amount,
            "confidence": max(0, min(100, int(confidence))),
            "notes": "; ".join(notes_parts),
        })

    return entries

