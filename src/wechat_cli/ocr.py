import csv
import hashlib
import io
import os
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

from .errors import AutomationError


class OCR:
    def __init__(self, language="chi_sim+eng", tessdata_dir=None, engine_mode=None, profile="fast"):
        self.language = language
        self.tessdata_dir = Path(tessdata_dir) if tessdata_dir else None
        self.engine_mode = engine_mode
        self.profile = profile
        self.cache = OrderedDict()
        self.calls = 0
        self.cache_hits = 0
        self.elapsed_ms = 0.0

    @property
    def model_available(self):
        if self.tessdata_dir is None:
            return True
        return all((self.tessdata_dir / f"{language}.traineddata").is_file()
                   for language in self.language.split("+"))

    def metrics(self):
        return {"profile": self.profile, "language": self.language,
                "tessdata_dir": str(self.tessdata_dir) if self.tessdata_dir else None,
                "model_available": self.model_available, "calls": self.calls,
                "cache_hits": self.cache_hits, "elapsed_ms": round(self.elapsed_ms, 3)}

    def words(self, image, psm=6):
        key = (image.size, psm, hashlib.blake2b(image.tobytes(), digest_size=16).digest())
        if key in self.cache:
            self.cache_hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        encoded = io.BytesIO()
        image.save(encoded, format="PNG")
        started = time.monotonic()
        command = ["tesseract", "stdin", "stdout", "-l", self.language]
        if self.tessdata_dir is not None:
            command.extend(["--tessdata-dir", str(self.tessdata_dir)])
        if self.engine_mode is not None:
            command.extend(["--oem", str(self.engine_mode)])
        command.extend(["--psm", str(psm), "tsv"])
        try:
            response = subprocess.run(
                command, input=encoded.getvalue(),
                capture_output=True, timeout=8,
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise AutomationError("OCR_UNAVAILABLE", str(error)) from error
        self.elapsed_ms += (time.monotonic() - started) * 1000
        self.calls += 1
        if response.returncode:
            raise AutomationError("OCR_FAILED", response.stderr.decode(errors="replace")[-500:])
        words = []
        for row in csv.DictReader(io.StringIO(response.stdout.decode("utf-8")), delimiter="\t"):
            if row["text"].strip() and float(row["conf"]) >= 20:
                words.append({"text": row["text"], "confidence": float(row["conf"]),
                              "rect": [int(row[name]) for name in ("left", "top", "width", "height")],
                              "line": [int(row[name]) for name in ("block_num", "par_num", "line_num")]})
        self.cache[key] = words
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return words

    def lines(self, image, psm=6, scale=1):
        if type(scale) is not int or scale < 1:
            raise ValueError("scale must be a positive integer")
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
        grouped = OrderedDict()
        for word in self.words(image, psm):
            grouped.setdefault(tuple(word["line"]), []).append(word)
        result = []
        for words in grouped.values():
            left = min(word["rect"][0] for word in words)
            top = min(word["rect"][1] for word in words)
            right = max(word["rect"][0] + word["rect"][2] for word in words)
            bottom = max(word["rect"][1] + word["rect"][3] for word in words)
            result.append({"text": " ".join(word["text"] for word in words),
                           "rect": [left // scale, top // scale,
                                    max(1, (right - left) // scale),
                                    max(1, (bottom - top) // scale)]})
        return result


class RapidOCRReader:
    """Optional high-accuracy Chinese reader for user-facing content."""

    profile = "rapidocr"
    language = "chinese+english"

    def __init__(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            self.factory = None
        else:
            self.factory = RapidOCR
        self.engine = None
        self.cache = OrderedDict()
        self.calls = 0
        self.cache_hits = 0
        self.elapsed_ms = 0.0

    @property
    def model_available(self):
        return self.factory is not None

    def metrics(self):
        return {"profile": self.profile, "language": self.language,
                "model_available": self.model_available, "loaded": self.engine is not None,
                "calls": self.calls, "cache_hits": self.cache_hits,
                "elapsed_ms": round(self.elapsed_ms, 3)}

    def lines(self, image, psm=6, scale=1):
        if type(scale) is not int or scale < 1:
            raise ValueError("scale must be a positive integer")
        if not self.model_available:
            raise AutomationError("OCR_UNAVAILABLE", "RapidOCR is not installed")
        if scale > 1:
            image = image.resize((image.width * scale, image.height * scale), Image.Resampling.LANCZOS)
        key = (image.size, psm, scale, hashlib.blake2b(image.tobytes(), digest_size=16).digest())
        if key in self.cache:
            self.cache_hits += 1
            self.cache.move_to_end(key)
            return self.cache[key]
        if self.engine is None:
            self.engine = self.factory()
        started = time.monotonic()
        try:
            result, _ = self.engine(np.asarray(image))
        except Exception as error:
            raise AutomationError("OCR_FAILED", str(error)) from error
        self.elapsed_ms += (time.monotonic() - started) * 1000
        self.calls += 1
        lines = []
        for item in result or []:
            points, text, confidence = item
            if not text.strip() or confidence < 0.4:
                continue
            left = min(point[0] for point in points)
            top = min(point[1] for point in points)
            right = max(point[0] for point in points)
            bottom = max(point[1] for point in points)
            lines.append({"text": text, "rect": [int(left) // scale, int(top) // scale,
                                                   max(1, int(right - left) // scale),
                                                   max(1, int(bottom - top) // scale)]})
        self.cache[key] = lines
        if len(self.cache) > 128:
            self.cache.popitem(last=False)
        return lines
