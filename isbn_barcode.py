import io
import logging
import re

import zxingcpp
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Book barcodes are EAN-13 starting with the ISBN prefix 978 or 979
ISBN_EAN = re.compile(r'97[89]\d{10}')


def decode_isbn(image_bytes):
    """Returns the ISBN from a photo of a book's barcode, or None."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image).convert("L")
    except Exception as e:
        logger.warning(f"Could not open image: {e}")
        return None

    # zxing-cpp already tries rotations; the enlarged copy helps small, compressed photos
    candidates = [image]
    if max(image.size) < 1600:
        candidates.append(image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS))
    for candidate in candidates:
        for barcode in zxingcpp.read_barcodes(candidate, formats=zxingcpp.BarcodeFormat.EAN13):
            if ISBN_EAN.fullmatch(barcode.text):
                return barcode.text
    return None
