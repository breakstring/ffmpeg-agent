import errno
import io
import json
import os
import sys
import warnings
from pathlib import Path

from PIL import Image, ImageCms, ImageColor, ImageOps

from media_service.errors import ServiceError
from media_service.models import ImageParams

FORMATS = {
    "jpeg": ("JPEG", "image/jpeg"),
    "png": ("PNG", "image/png"),
    "webp": ("WEBP", "image/webp"),
}
SAVE_OPTIONS = {
    "jpeg": {"quality": 90, "subsampling": 2},
    "png": {"compress_level": 6},
    "webp": {"quality": 85, "method": 4},
}
MODES = {"1", "L", "LA", "P", "RGB", "RGBA"}


def _read_image(fd: int, max_pixels: int) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = max_pixels
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), "rb") as source_handle, Image.open(source_handle) as source:
        if source.format not in {"JPEG", "PNG", "WEBP"} or getattr(source, "n_frames", 1) != 1:
            raise ServiceError("UNSUPPORTED_MEDIA")
        if source.width * source.height > max_pixels:
            raise ServiceError("INPUT_TOO_LARGE")
        if source.mode not in MODES:
            raise ServiceError("UNSUPPORTED_MEDIA")
        icc = source.info.get("icc_profile")
        image = ImageOps.exif_transpose(source)
        image.load()
        if icc:
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                if getattr(profile.profile, "xcolor_space", "").strip() != "RGB":
                    raise ServiceError("UNSUPPORTED_MEDIA")
                alpha = (
                    image.convert("RGBA").getchannel("A")
                    if image.mode in {"LA", "RGBA", "P"}
                    else None
                )
                converted = ImageCms.profileToProfile(
                    image.convert("RGB"), profile, ImageCms.createProfile("sRGB"), outputMode="RGB"
                )
                if converted is None:
                    raise ServiceError("UNSUPPORTED_MEDIA")
                image = converted
                if alpha is not None:
                    image.putalpha(alpha)
            except OSError, ImageCms.PyCMSError:
                raise ServiceError("UNSUPPORTED_MEDIA") from None
        elif image.mode in {"1", "L"}:
            image = image.convert("RGB")
        elif image.mode in {"LA", "P"}:
            image = image.convert("RGBA")
        return image


def _flatten(image: Image.Image, color: str) -> Image.Image:
    background = Image.new("RGB", image.size, ImageColor.getrgb(color))
    if image.mode == "RGBA":
        background.paste(image, mask=image.getchannel("A"))
    else:
        background.paste(image.convert("RGB"))
    return background


def _save_image(image: Image.Image, destination: Path, format: str, options: dict) -> dict:
    image.info.clear()
    format_name, content_type = FORMATS[format]
    with destination.open("xb") as output:
        image.save(output, format=format_name, **options)
        output.flush()
        os.fsync(output.fileno())
    # 重新解码输出，不能只凭save未抛异常就发布。
    with Image.open(destination) as check:
        check.load()
        if check.size != image.size or check.format != format_name or check.getexif():
            raise ServiceError("INVALID_MEDIA")
    return {
        "type": "image",
        "format": format,
        "content_type": content_type,
        "width": image.width,
        "height": image.height,
        "color_space": "srgb",
    }


def resize(fd: int, destination: Path, params: dict, max_pixels: int) -> dict:
    image = _read_image(fd, max_pixels)
    width, height = params["width"], params["height"]
    fit = params["fit"]
    resampling = {
        "lanczos": Image.Resampling.LANCZOS,
        "bicubic": Image.Resampling.BICUBIC,
        "bilinear": Image.Resampling.BILINEAR,
        "nearest": Image.Resampling.NEAREST,
    }[params["resampling"]]
    if fit == "cover":
        if width is None or height is None:
            raise ServiceError("UNSUPPORTED_OPERATION")
        if width * height > max_pixels:
            raise ServiceError("OUTPUT_LIMIT")
        if not params["allow_upscale"] and (width > image.width or height > image.height):
            raise ServiceError("UNSUPPORTED_MEDIA")
        image = ImageOps.fit(
            image, (width, height), resampling, centering=(params["crop_x"], params["crop_y"])
        )
    elif width is not None or height is not None:
        factors = []
        if width is not None:
            factors.append(width / image.width)
        if height is not None:
            factors.append(height / image.height)
        factor = min(factors)
        if not params["allow_upscale"]:
            factor = min(factor, 1)
        size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
        if size[0] * size[1] > max_pixels:
            raise ServiceError("OUTPUT_LIMIT")
        if fit == "pad":
            if width is None or height is None:
                raise ServiceError("UNSUPPORTED_OPERATION")
            if width * height > max_pixels:
                raise ServiceError("OUTPUT_LIMIT")
        image = image.resize(size, resampling)
        if fit == "pad" and width is not None and height is not None:
            background = Image.new("RGB", (width, height), ImageColor.getrgb(params["background"]))
            position = (
                round((width - image.width) * params["crop_x"]),
                round((height - image.height) * params["crop_y"]),
            )
            background.paste(
                image.convert("RGB"),
                position,
                image.getchannel("A") if image.mode == "RGBA" else None,
            )
            image = background
    if params["format"] == "jpeg":
        image = _flatten(image, params["background"])
        options = {
            "quality": params["quality"],
            "subsampling": params["jpeg_subsampling"],
            "progressive": params["progressive"],
        }
    elif params["format"] == "png":
        options = {"compress_level": params["compression_level"]}
    else:
        options = {
            "quality": params["quality"],
            "lossless": params["lossless"],
            "method": params["method"],
        }
    return _save_image(image, destination, params["format"], options)


def main() -> None:
    try:
        fd, destination, raw_params, max_pixels = sys.argv[1:5]
        params = ImageParams.model_validate_json(raw_params).model_dump()
        media = resize(int(fd), Path(destination), params, int(max_pixels))
        print(json.dumps({"media": media}, separators=(",", ":")))
    except ServiceError as error:
        print(json.dumps({"error": error.code}))
        raise SystemExit(1) from None
    except Image.DecompressionBombWarning, Image.DecompressionBombError:
        print(json.dumps({"error": "INPUT_TOO_LARGE"}))
        raise SystemExit(1) from None
    except OSError as error:
        code = "DISK_FULL" if error.errno in {errno.ENOSPC, errno.EDQUOT} else "INVALID_MEDIA"
        print(json.dumps({"error": code}))
        raise SystemExit(1) from None
    except ValueError, SyntaxError:
        print(json.dumps({"error": "INVALID_MEDIA"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
