import hashlib
import re

import numpy as np
from PIL import Image


def runs(mask, minimum=1):
    padded = np.pad(np.asarray(mask, dtype=np.int8), (1, 1))
    starts = np.flatnonzero(np.diff(padded) == 1)
    ends = np.flatnonzero(np.diff(padded) == -1)
    return [(int(start), int(end)) for start, end in zip(starts, ends)
            if end - start >= minimum]


def text_identity(text):
    normalized = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff])", "", normalized)


def bubble_regions(image):
    pixels = np.asarray(image)
    height, width = pixels.shape[:2]
    regions = []
    if width < 700:
        return regions
    for direction, column in (("incoming", 78), ("outgoing", width - 85)):
        stripe = pixels[:, column:column + 4]
        if direction == "incoming":
            mask = (stripe.min(axis=2) >= 250).mean(axis=1) > 0.8
        else:
            mask = ((stripe[:, :, 1] > 170) & (stripe[:, :, 0] < 200)
                    & (stripe[:, :, 2] < 160)).mean(axis=1) > 0.8
        for top, bottom in runs(mask, minimum=15):
            if bottom - top > height * 0.8:
                continue
            spans = []
            sample_rows = list(range(top, min(bottom, top + 5))) + list(range(max(top, bottom - 5), bottom))
            for row in sample_rows:
                middle = pixels[row]
                if direction == "incoming":
                    row_mask = middle.min(axis=1) >= 250
                else:
                    row_mask = ((middle[:, 1] > 170) & (middle[:, 0] < 200) & (middle[:, 2] < 160))
                spans.extend((left, right) for left, right in runs(row_mask, minimum=20)
                             if left <= column < right)
            if not spans:
                continue
            left, right = max(spans, key=lambda span: span[1] - span[0])
            regions.append({"direction": direction,
                            "rect": [max(0, left - 2), max(0, top - 3),
                                     min(width, right + 2) - max(0, left - 2),
                                     min(height, bottom + 3) - max(0, top - 3)]})
    return sorted(regions, key=lambda region: region["rect"][1])


def image_digest(image):
    return hashlib.blake2b(image.tobytes(), digest_size=16).hexdigest()


def editor_has_content(image):
    pixels = np.asarray(image)
    dark = pixels.max(axis=2) < 180
    return int(dark.sum()) > 40


def merge_message_pages(older, newer):
    older_digests = [message["visual_digest"] for message in older]
    newer_digests = [message["visual_digest"] for message in newer]
    for overlap in range(min(len(older), len(newer)), 0, -1):
        if older_digests[-overlap:] == newer_digests[:overlap]:
            return older[:-overlap] + newer, len(older) - overlap
    return older + newer, len(older)


def adjacent_bubble(bubbles, target):
    left, top, width, height = target["rect"]
    bottom = top + height
    matches = [bubble for bubble in bubbles if bubble is not target
               and bubble["direction"] == target["direction"]
               and abs(bubble["rect"][0] - left) <= 25
               and bottom - 2 <= bubble["rect"][1] <= bottom + 10]
    return matches[0] if len(matches) == 1 else None


def avatar_template_match(image, template, size=20, stride=2):
    """Return the lowest RGB mean-error match of one square avatar template."""
    source = np.asarray(image, dtype=np.uint8)
    reference = np.asarray(template.resize((size, size)), dtype=np.int16)
    template_width = max(1, template.width)
    template_height = max(1, template.height)
    best_score = float("inf")
    best_rect = None
    for top in range(0, max(1, source.shape[0] - template_height + 1), stride):
        for left in range(0, max(1, source.shape[1] - template_width + 1), stride):
            crop = source[top:top + template_height, left:left + template_width]
            if crop.shape[:2] != (template_height, template_width):
                continue
            candidate = np.asarray(Image.fromarray(crop).resize((size, size)), dtype=np.int16)
            score = float(np.abs(candidate - reference).mean())
            if score < best_score:
                best_score = score
                best_rect = (left, top, template_width, template_height)
    return best_score, best_rect
