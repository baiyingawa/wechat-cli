import os


def save_private_image(image, path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            image.save(stream, format="PNG")
    except Exception:
        path.unlink(missing_ok=True)
        raise
