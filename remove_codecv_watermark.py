#!/usr/bin/env python3
"""Remove CodeCV's watermark from exported resumes.

Supports two watermark generations:

1. Vector tiles: ``/Pattern`` color-space fills painted as tiled
   ``CodeCV简历`` text (older exports).
2. Raster overlay (newer exports): a single full-page image XObject with a
   transparency SMask, drawn on top of all content.  It is detected by
   tracking the CTM through the content stream and removing image draws
   that (a) reference an image with an /SMask and (b) cover (almost) the
   whole page.

Neither path rasterizes the PDF, so original text, layout and page size
are preserved.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream, DecodedStreamObject, NameObject


DEFAULT_OUTPUT_SUFFIX = ".clean.pdf"

# How much of the page a transparent image must cover to be considered a
# watermark overlay (avatars / logos cover only a small fraction).
FULL_PAGE_COVERAGE = 0.90

# Content-stream operators that paint something.  A q..Q block containing
# none of these can be deleted without any visual effect.
MARKING_OPS = {
    b"f", b"f*", b"B", b"B*", b"b", b"b*", b"S", b"s",  # path painting
    b"Do", b"sh",                                        # xobject / shading
    b"Tj", b"TJ", b"'", b'"',                            # text showing
    b"BI",                                               # inline image
}


def _is_name(value, expected: str) -> bool:
    return isinstance(value, NameObject) and str(value) == expected


def _first_name(operands: Iterable[object]) -> str | None:
    for operand in operands:
        if isinstance(operand, NameObject):
            return str(operand)
    return None


# ---------------------------------------------------------------------------
# Generation 1: tiled /Pattern vector watermark
# ---------------------------------------------------------------------------

def _is_pattern_fill(operations, index: int) -> tuple[int, str | None]:
    """Return the operation count for a CodeCV watermark fill, or zero."""
    if index + 5 >= len(operations):
        return 0, None

    op0, op1, op2, op3 = operations[index : index + 4]
    uses_pattern_space = (
        op0[1] == b"CS"
        and op1[1] == b"cs"
        and len(op0[0]) == 1
        and len(op1[0]) == 1
        and _is_name(op0[0][0], "/Pattern")
        and _is_name(op1[0][0], "/Pattern")
    )
    if not uses_pattern_space or op2[1] != b"SCN" or op3[1] != b"scn":
        return 0, None

    stroke_pattern = _first_name(op2[0])
    fill_pattern = _first_name(op3[0])
    if stroke_pattern is None or stroke_pattern != fill_pattern:
        return 0, None

    fill_index = index + 4
    if fill_index < len(operations) and operations[fill_index][1] == b"gs":
        fill_index += 1
    if fill_index + 1 >= len(operations):
        return 0, None

    rect_op, fill_op = operations[fill_index : fill_index + 2]
    fills_rectangle = rect_op[1] == b"re" and fill_op[1] in {b"f", b"f*"}
    if not fills_rectangle:
        return 0, None

    return fill_index + 2 - index, fill_pattern


def _remove_pattern_watermarks(operations) -> tuple[list, set]:
    """Strip pattern-fill watermark operations from an operation list."""
    new_operations = []
    removed_patterns: set[str] = set()
    index = 0

    while index < len(operations):
        matched_count, pattern_name = _is_pattern_fill(operations, index)
        if matched_count:
            removed_patterns.add(pattern_name)
            index += matched_count
            continue

        new_operations.append(operations[index])
        index += 1

    return new_operations, removed_patterns


def _drop_patterns_from_resources(page, removed_patterns: set[str]) -> None:
    if not removed_patterns:
        return
    resources = page.get("/Resources")
    patterns = resources.get("/Pattern") if resources else None
    if patterns:
        for pattern_name in removed_patterns:
            patterns.pop(NameObject(pattern_name), None)
        if not patterns:
            resources.pop(NameObject("/Pattern"), None)


# ---------------------------------------------------------------------------
# Generation 2: full-page rasterized watermark image
# ---------------------------------------------------------------------------

def _mat_mul(m, n):
    """Concatenate PDF transformation matrices (row-vector convention)."""
    a1, b1, c1, d1, e1, f1 = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def _apply(ctm, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = ctm
    return a * x + c * y + e, b * x + d * y + f


def _unit_square_bbox(ctm) -> tuple[float, float, float, float]:
    points = [_apply(ctm, 0, 0), _apply(ctm, 1, 0),
              _apply(ctm, 0, 1), _apply(ctm, 1, 1)]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _covers_page(bbox, mediabox, threshold: float = FULL_PAGE_COVERAGE) -> bool:
    ix0, iy0, ix1, iy1 = bbox
    px0, py0, px1, py1 = (float(v) for v in mediabox)
    overlap_w = max(0.0, min(ix1, px1) - max(ix0, px0))
    overlap_h = max(0.0, min(iy1, py1) - max(iy0, py0))
    page_area = abs((px1 - px0) * (py1 - py0))
    if page_area == 0:
        return False
    return (overlap_w * overlap_h) / page_area >= threshold


def _page_xobjects(page) -> dict:
    try:
        xobjects = page["/Resources"]["/XObject"]
    except KeyError:
        return {}
    xobjects = xobjects.get_object()
    return {str(name): obj.get_object() for name, obj in xobjects.items()}


def _find_watermark_image_dos(operations, page) -> list[tuple[int, str]]:
    """Locate ``Do`` ops painting a full-page transparent image overlay."""
    xobjects = _page_xobjects(page)
    if not xobjects:
        return []

    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    ctm = identity
    stack: list = []
    hits: list[tuple[int, str]] = []

    for index, (operands, operator) in enumerate(operations):
        if operator == b"q":
            stack.append(ctm)
        elif operator == b"Q":
            if stack:
                ctm = stack.pop()
        elif operator == b"cm":
            ctm = _mat_mul(tuple(float(v) for v in operands), ctm)
        elif operator == b"Do":
            name = _first_name(operands)
            if name is None or name not in xobjects:
                continue
            xobject = xobjects[name]
            if xobject.get("/Subtype") != "/Image":
                continue
            if xobject.get("/SMask") is None:
                continue
            if _covers_page(_unit_square_bbox(ctm), page.mediabox):
                hits.append((index, name))

    return hits


def _innermost_enclosing_q(operations, index: int) -> int | None:
    depth = 0
    for i in range(index - 1, -1, -1):
        operator = operations[i][1]
        if operator == b"Q":
            depth += 1
        elif operator == b"q":
            if depth == 0:
                return i
            depth -= 1
    return None


def _matching_q(operations, q_index: int) -> int | None:
    depth = 0
    for i in range(q_index + 1, len(operations)):
        operator = operations[i][1]
        if operator == b"q":
            depth += 1
        elif operator == b"Q":
            if depth == 0:
                return i
            depth -= 1
    return None


def _remove_dos_with_blocks(operations, do_indices: list[int]) -> list:
    """Delete ``Do`` ops plus their enclosing q..Q blocks when those blocks
    paint nothing else (graphics-state setup and clipping are dropped too)."""
    remove: set[int] = set(do_indices)

    for do_index in do_indices:
        start = end = do_index
        while True:
            q_index = _innermost_enclosing_q(operations, start)
            if q_index is None:
                break
            q_end = _matching_q(operations, q_index)
            if q_end is None:
                break
            head_ok = all(
                operations[i][1] not in MARKING_OPS for i in range(q_index, start)
            )
            tail_ok = all(
                operations[i][1] not in MARKING_OPS for i in range(end + 1, q_end + 1)
            )
            if not (head_ok and tail_ok):
                break
            start, end = q_index, q_end
        remove.update(range(start, end + 1))

    return [op for i, op in enumerate(operations) if i not in remove]


def _drop_watermark_images_from_resources(page, removed_names: set[str]) -> None:
    if not removed_names:
        return
    try:
        xobjects = page["/Resources"]["/XObject"].get_object()
    except KeyError:
        return
    for name in removed_names:
        xobjects.pop(NameObject(name), None)
    if not xobjects:
        page["/Resources"].get_object().pop(NameObject("/XObject"), None)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _remove_page_watermark(page, pdf) -> tuple[int, int]:
    """Clean one page. Returns (patterns_removed, watermark_images_removed)."""
    contents = page.get_contents()
    if contents is None:
        return 0, 0

    stream = ContentStream(contents, pdf)
    operations = stream.operations

    # Generation 1: pattern fills.
    operations, removed_patterns = _remove_pattern_watermarks(operations)
    _drop_patterns_from_resources(page, removed_patterns)

    # Generation 2: full-page transparent watermark image.
    hits = _find_watermark_image_dos(operations, page)
    removed_images: set[str] = set()
    if hits:
        operations = _remove_dos_with_blocks(operations, [i for i, _ in hits])
        removed_images = {name for _, name in hits}
        _drop_watermark_images_from_resources(page, removed_images)

    if removed_patterns or removed_images:
        stream.operations = operations
        replacement = DecodedStreamObject()
        replacement.set_data(stream.get_data())
        page.replace_contents(replacement)

    return len(removed_patterns), len(removed_images)


def remove_watermark(input_pdf: str | Path, output_pdf: str | Path) -> dict:
    """Remove CodeCV watermarks and write a cleaned PDF.

    Returns counts of removed pattern fills and watermark images.
    """
    input_path = Path(input_pdf)
    output_path = Path(output_pdf)

    reader = PdfReader(str(input_path))
    writer = PdfWriter(clone_from=reader)

    total_patterns = 0
    total_images = 0
    for page in writer.pages:
        patterns, images = _remove_page_watermark(page, writer)
        total_patterns += patterns
        total_images += images

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        writer.write(handle)

    return {"patterns": total_patterns, "images": total_images}


def default_output_path(input_pdf: Path) -> Path:
    return input_pdf.with_name(input_pdf.stem + DEFAULT_OUTPUT_SUFFIX)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove CodeCV's watermark from an exported resume PDF."
    )
    parser.add_argument("input_pdf", type=Path, help="Path to the exported CodeCV PDF")
    parser.add_argument(
        "output_pdf",
        type=Path,
        nargs="?",
        help="Cleaned PDF path. Defaults to '<input>.clean.pdf'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_pdf = args.output_pdf or default_output_path(args.input_pdf)
    counts = remove_watermark(args.input_pdf, output_pdf)
    print(
        f"Removed {counts['patterns']} pattern fill(s) and "
        f"{counts['images']} watermark image(s)."
    )
    print(f"Wrote: {output_pdf}")
    return 0 if counts["patterns"] or counts["images"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
