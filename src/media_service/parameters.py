import re
from fractions import Fraction
from typing import Annotated, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, validate_default=True)


VIDEO_ENCODERS = {"h264": "libx264", "hevc": "libx265", "vp9": "libvpx-vp9", "av1": "libsvtav1"}
AUDIO_ENCODERS = {"aac": "aac", "mp3": "libmp3lame", "opus": "libopus", "flac": "flac",
                  "pcm": "pcm_s16le"}
VIDEO_CONTAINERS = {"mp4": ("h264", "hevc", "av1"), "mkv": tuple(VIDEO_ENCODERS),
                    "webm": ("vp9", "av1")}
AUDIO_CONTAINERS = {"m4a": ("aac",), "mp3": ("mp3",), "ogg": ("opus",),
                    "flac": ("flac",), "wav": ("pcm",), "mka": tuple(AUDIO_ENCODERS)}
VIDEO_AUDIO = {"mp4": ("aac", "mp3"), "mkv": tuple(AUDIO_ENCODERS), "webm": ("opus",)}
AUDIO_RATES = {
    "aac": (7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000),
    "mp3": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
    "opus": (48000,),
}
LAYOUT_CHANNELS = {"mono": 1, "stereo": 2, "2.1": 3, "3.0": 3, "4.0": 4, "5.1": 6, "7.1": 8}
MUXERS = {"mp4": "mp4", "mkv": "matroska", "webm": "webm", "m4a": "ipod",
          "mp3": "mp3", "ogg": "ogg", "flac": "flac", "wav": "wav", "mka": "matroska"}


def mp3_bitrates(sample_rate: int) -> tuple[int, ...]:
    if sample_rate < 16000:
        values = (8, 16, 24, 32, 40, 48, 56, 64)
    elif sample_rate < 32000:
        values = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
    else:
        values = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
    return tuple(value * 1000 for value in values)


def audio_bitrate_supported(codec: str, sample_rate: int, channels: int, bitrate: int) -> bool:
    if codec == "mp3":
        return bitrate in mp3_bitrates(sample_rate)
    if codec == "aac":
        return bitrate <= 6 * sample_rate * channels
    return True


def available_parameters(engine: dict) -> dict:
    encoders = set(engine.get("available_encoders", []))
    muxers = set(engine.get("available_muxers", []))
    videos = {key: value for key, value in VIDEO_ENCODERS.items() if value in encoders}
    audios = {key: value for key, value in AUDIO_ENCODERS.items() if value in encoders}
    return {
        "video_codecs": videos, "audio_codecs": audios,
        "video_containers": {key: [c for c in values if c in videos]
                             for key, values in VIDEO_CONTAINERS.items() if MUXERS[key] in muxers},
        "audio_containers": {key: [c for c in values if c in audios]
                             for key, values in AUDIO_CONTAINERS.items() if MUXERS[key] in muxers},
        "video_audio_codecs": {key: [c for c in values if c in audios]
                               for key, values in VIDEO_AUDIO.items() if MUXERS[key] in muxers},
        "audio_sample_rates": AUDIO_RATES,
        "mp3_bitrates_bps": {str(rate): mp3_bitrates(rate) for rate in AUDIO_RATES["mp3"]},
        "aac_max_bitrate_rule": "6 * sample_rate * channels",
        "pcm_sample_formats": [key for key, codec in {"s16": "pcm_s16le", "s24": "pcm_s24le",
                                                     "f32": "pcm_f32le"}.items() if codec in encoders],
        "video_crf_ranges": {key: [0, 51 if key in ("h264", "hevc") else 63] for key in videos},
        "audio_quality_ranges": {"aac": [0.1, 5], "mp3": [0, 9]},
        "channel_layouts": LAYOUT_CHANNELS,
        "video_pixel_formats": ["yuv420p"], "hardware_acceleration": False,
        "hdr": False, "max_bit_depth": 8,
        "av1_min_dimensions": [64, 64],
    }


class TimeRange(Parameters):
    start_ms: int = Field(default=0, ge=0)
    end_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class Resize(Parameters):
    width: int | None = Field(default=None, ge=1, le=65535)
    height: int | None = Field(default=None, ge=1, le=65535)
    fit: Literal["contain", "cover", "pad"] = "contain"
    allow_upscale: bool = False
    background: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    crop_x: float = Field(default=0.5, ge=0, le=1)
    crop_y: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def geometry(self):
        if self.fit != "contain" and (self.width is None or self.height is None):
            raise ValueError("cover/pad require both dimensions")
        if self.fit != "cover" and (self.crop_x != 0.5 or self.crop_y != 0.5):
            raise ValueError("crop position requires cover")
        return self


class FrameRate(Parameters):
    mode: Literal["source", "constant", "max"] = "source"
    value: str | int | float | None = None

    @model_validator(mode="before")
    @classmethod
    def default_value(cls, value):
        if isinstance(value, dict) and "value" in value and "mode" not in value:
            return value | {"mode": "constant"}
        return value

    @model_validator(mode="after")
    def rational(self):
        if self.mode == "source":
            if self.value is not None:
                raise ValueError("source frame rate cannot specify value")
        else:
            # Fraction 接受指数记法，会同步计算任意大的幂；公共参数只接受有界十进制/分数。
            value = self.value
            if isinstance(value, str):
                if not re.fullmatch(r"[0-9]{1,9}(?:/[0-9]{1,9}|\.[0-9]{1,9})?", value):
                    raise ValueError("frame rate must use bounded decimal or fraction notation")
            elif not isinstance(value, (int, float)) or not 0 < value <= 999999999:
                raise ValueError("frame rate must be a positive finite number")
            try:
                rate = Fraction(str(value))
            except (ValueError, ZeroDivisionError):
                raise ValueError("frame rate must be a positive rational") from None
            if rate <= 0 or max(rate.numerator, rate.denominator) > 999999999:
                raise ValueError("frame rate must be positive")
            self.value = f"{rate.numerator}/{rate.denominator}"
        return self


class CRF(Parameters):
    mode: Literal["crf"] = "crf"
    value: float = Field(default=23.0, ge=0, le=63)


class VideoBitrate(Parameters):
    mode: Literal["bitrate"] = "bitrate"
    bitrate_bps: int = Field(gt=0)
    maxrate_bps: int | None = Field(default=None, gt=0)
    buffer_size_bits: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def buffer(self):
        if (self.maxrate_bps is None) != (self.buffer_size_bits is None):
            raise ValueError("peak bitrate and buffer size must be specified together")
        if self.maxrate_bps is not None and self.maxrate_bps < self.bitrate_bps:
            raise ValueError("peak bitrate must not be below target bitrate")
        return self


class AudioBitrate(Parameters):
    mode: Literal["bitrate"] = "bitrate"
    bitrate_bps: int = Field(gt=0)


class AudioQuality(Parameters):
    mode: Literal["quality"] = "quality"
    value: float = Field(ge=0, le=9)


class AudioAutoBitrate(Parameters):
    mode: Literal["auto"] = "auto"


AudioMode = TypeVar("AudioMode", bound=str, default=Literal["encode", "copy", "drop"])


class AudioEncoding(Parameters, Generic[AudioMode]):
    mode: AudioMode = cast(AudioMode, "encode")
    audio_stream: int = Field(default=0, ge=0, le=31)
    codec: Literal["aac", "mp3", "opus", "flac", "pcm"] | None = "aac"
    sample_rate: Annotated[int, Field(ge=1, le=384000)] | Literal["auto", "source"] | None = "auto"
    channels: Annotated[int, Field(ge=1, le=8)] | Literal["auto", "source"] | None = "auto"
    channel_layout: Literal["mono", "stereo", "2.1", "3.0", "4.0", "5.1", "7.1"] | None = None
    sample_format: Literal["s16", "s24", "f32"] | None = None
    rate_control: Annotated[AudioBitrate | AudioQuality | AudioAutoBitrate,
                            Field(discriminator="mode")] | None = None
    compression_level: int | None = Field(default=None, ge=0, le=12)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, value):
        if not isinstance(value, dict):
            return value
        value = value.copy()
        encoding = ("codec", "sample_rate", "channels", "channel_layout", "sample_format",
                    "rate_control", "compression_level")
        if value.get("mode", "encode") != "encode":
            if any(value.get(k) is not None for k in encoding):
                raise ValueError("copy/drop cannot specify encoding parameters")
            return value | dict.fromkeys(encoding)
        codec = value.get("codec", "aac")
        layout = value.get("channel_layout")
        if codec is not None and not isinstance(codec, str):
            raise ValueError("codec must be a string")
        if layout is not None and not isinstance(layout, str):
            raise ValueError("channel layout must be a string")
        if layout in LAYOUT_CHANNELS:
            value.setdefault("channels", LAYOUT_CHANNELS[value["channel_layout"]])
        if "rate_control" not in value and codec in ("aac", "mp3", "opus"):
            value["rate_control"] = {"mode": "auto"}
        if codec == "pcm":
            value.setdefault("sample_format", "s16")
        if codec == "flac":
            value.setdefault("compression_level", 5)
        return value

    @model_validator(mode="after")
    def compatible(self):
        if self.mode != "encode":
            return self
        if self.codec is None or self.channels is None or self.sample_rate is None:
            raise ValueError("encoding requires codec, sample rate and channels")
        if self.codec in AUDIO_RATES and isinstance(self.sample_rate, int):
            if self.sample_rate not in AUDIO_RATES[self.codec]:
                raise ValueError("sample rate is incompatible with codec")
        if self.codec == "mp3" and isinstance(self.channels, int) and self.channels > 2:
            raise ValueError("mp3 supports mono/stereo only")
        if self.codec == "opus":
            if isinstance(self.channels, int) and self.channels not in (1, 2, 3, 6, 8):
                raise ValueError("unsupported opus channel count")
            if self.channel_layout in ("2.1", "4.0"):
                raise ValueError("unsupported opus channel layout")
        if self.channel_layout and self.channels != LAYOUT_CHANNELS[self.channel_layout]:
            raise ValueError("channel layout conflicts with channels")
        if self.codec in ("pcm", "flac"):
            if self.rate_control is not None:
                raise ValueError("lossless audio does not use bitrate or quality controls")
        elif self.rate_control is None:
            raise ValueError("lossy audio requires rate control")
        if isinstance(self.rate_control, AudioQuality):
            if self.codec == "aac" and not 0.1 <= self.rate_control.value <= 5:
                raise ValueError("aac quality must be between 0.1 and 5")
            if self.codec not in ("aac", "mp3"):
                raise ValueError("quality mode is not supported by this codec")
        if isinstance(self.rate_control, AudioBitrate):
            if isinstance(self.sample_rate, int) and isinstance(self.channels, int):
                if not audio_bitrate_supported(self.codec, self.sample_rate, self.channels,
                                               self.rate_control.bitrate_bps):
                    raise ValueError("bitrate is incompatible with audio codec and sample rate")
        if self.sample_format is not None and self.codec != "pcm":
            raise ValueError("sample format applies to pcm only")
        if self.compression_level is not None and self.codec != "flac":
            raise ValueError("compression level applies to flac only")
        return self


class AudioParams(AudioEncoding[Literal["encode", "copy"]]):
    preset: Literal["aac_stereo_128k_v1", "wav_mono_16k_v1"] | None = None
    container: Literal["m4a", "mp3", "ogg", "flac", "wav", "mka"] = "m4a"
    range: TimeRange = Field(default_factory=TimeRange)

    @model_validator(mode="before")
    @classmethod
    def preset_defaults(cls, value):
        if not isinstance(value, dict):
            return value
        value = value.copy()
        if value.get("mode") == "copy":
            if value.get("preset") is not None:
                raise ValueError("copy cannot apply an encoding preset")
            value.setdefault("container", "mka")
            return value
        if value.get("preset") == "wav_mono_16k_v1" and value.get("codec", "pcm") == "pcm":
            defaults = {"codec": "pcm", "sample_rate": 16000, "sample_format": "s16"}
            if value.get("channel_layout") is None:
                defaults["channels"] = 1
            value = defaults | value
        elif value.get("preset") == "aac_stereo_128k_v1":
            defaults: dict = {"sample_rate": 48000}
            if value.get("channel_layout") is None:
                defaults["channels"] = 2
            if value.get("codec", "aac") == "aac":
                defaults["rate_control"] = {"mode": "bitrate", "bitrate_bps": 128000}
            value = defaults | value
        codec = value.get("codec", "aac")
        if codec is not None and not isinstance(codec, str):
            raise ValueError("codec must be a string")
        value.setdefault("container", {"aac": "m4a", "mp3": "mp3", "opus": "ogg",
                                       "pcm": "wav", "flac": "flac"}.get(codec, "m4a"))
        return value

    @model_validator(mode="after")
    def output_compatible(self):
        if self.mode == "copy":
            if self.range.start_ms or self.range.end_ms is not None:
                raise ValueError("copy does not support exact range trimming")
        elif self.codec not in AUDIO_CONTAINERS[self.container]:
            raise ValueError("audio codec is incompatible with container")
        return self


class VideoParams(Parameters):
    preset: Literal["web_720p_v1"] | None = None
    container: Literal["mp4", "mkv", "webm"] = "mp4"
    codec: Literal["h264", "hevc", "vp9", "av1"] = "h264"
    video_stream: int = Field(default=0, ge=0, le=31)
    resize: Resize = Field(default_factory=Resize)
    frame_rate: FrameRate = Field(default_factory=FrameRate)
    rate_control: Annotated[CRF | VideoBitrate, Field(discriminator="mode")] = Field(default_factory=CRF)
    speed: Literal["fast", "medium", "slow"] = "medium"
    pixel_format: Literal["yuv420p"] = "yuv420p"
    audio: AudioEncoding = Field(default_factory=lambda: AudioEncoding.model_validate({}))
    range: TimeRange = Field(default_factory=TimeRange)

    @model_validator(mode="before")
    @classmethod
    def codec_defaults(cls, value):
        if not isinstance(value, dict):
            return value
        value = value.copy()
        if value.get("preset") == "web_720p_v1":
            value.setdefault("resize", {"width": 1280, "height": 720})
            value.setdefault("frame_rate", {"mode": "constant", "value": "30/1"})
            if value.get("container", "mp4") != "webm" and value.get("codec", "h264") in ("h264", "hevc"):
                audio = value.get("audio", {})
                if isinstance(audio, dict) and audio.get("mode", "encode") == "encode":
                    audio_defaults: dict = {"sample_rate": 48000}
                    if audio.get("channel_layout") is None:
                        audio_defaults["channels"] = 2
                    if audio.get("codec", "aac") == "aac":
                        audio_defaults["rate_control"] = {"mode": "bitrate", "bitrate_bps": 128000}
                    value["audio"] = audio_defaults | audio
        codec = value.get("codec", "h264")
        value.setdefault("container", "webm" if codec in ("vp9", "av1") else "mp4")
        if codec in ("vp9", "av1"):
            value.setdefault("rate_control", {"mode": "crf", "value": 32})
        audio = value.get("audio", {})
        if value["container"] == "webm" and isinstance(audio, dict):
            if audio.get("mode", "encode") == "encode":
                value["audio"] = {"codec": "opus"} | audio
        return value

    @model_validator(mode="after")
    def compatible(self):
        if self.codec not in VIDEO_CONTAINERS[self.container]:
            raise ValueError("video codec is incompatible with container")
        if isinstance(self.rate_control, CRF) and self.codec in ("h264", "hevc"):
            if self.rate_control.value > 51:
                raise ValueError("h264/hevc crf must not exceed 51")
        if isinstance(self.rate_control, CRF) and self.codec in ("vp9", "av1"):
            if not float(self.rate_control.value).is_integer():
                raise ValueError("vp9/av1 crf must be an integer")
        if self.codec == "av1" and isinstance(self.rate_control, VideoBitrate):
            if self.rate_control.maxrate_bps is not None:
                raise ValueError("av1 target bitrate does not support peak bitrate controls")
        if self.audio.mode == "encode" and self.audio.codec not in VIDEO_AUDIO[self.container]:
            raise ValueError("audio codec is incompatible with video container")
        if self.audio.mode == "copy" and (self.range.start_ms or self.range.end_ms is not None):
            raise ValueError("copy audio does not support exact range trimming")
        if self.resize.fit != "contain":
            if any(n is not None and n % 2 for n in (self.resize.width, self.resize.height)):
                raise ValueError("exact video dimensions must be even")
        return self


class ImageParams(Resize):
    background: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    resampling: Literal["lanczos", "bicubic", "bilinear", "nearest"] = "lanczos"
    format: Literal["jpeg", "png", "webp"] = "jpeg"
    quality: int | None = Field(default=None, ge=0, le=100)
    compression_level: int | None = Field(default=None, ge=0, le=9)
    jpeg_subsampling: Literal[0, 1, 2] | None = None
    progressive: bool | None = None
    lossless: bool | None = None
    method: int | None = Field(default=None, ge=0, le=6)

    @model_validator(mode="before")
    @classmethod
    def encoding_defaults(cls, value):
        if not isinstance(value, dict):
            return value
        value = value.copy()
        fmt = value.get("format", "jpeg")
        if not isinstance(fmt, str):
            raise ValueError("image format must be a string")
        defaults = {"jpeg": {"quality": 90, "jpeg_subsampling": 2, "progressive": False},
                    "png": {"compression_level": 6},
                    "webp": {"quality": 85, "lossless": False, "method": 4}}
        for key, default in defaults.get(fmt, {}).items():
            value.setdefault(key, default)
        return value

    @model_validator(mode="after")
    def output_parameters(self):
        groups = {"jpeg": ("quality", "jpeg_subsampling", "progressive"),
                  "png": ("compression_level",), "webp": ("quality", "lossless", "method")}
        for name in ("quality", "jpeg_subsampling", "progressive", "compression_level", "lossless", "method"):
            if name not in groups[self.format] and getattr(self, name) is not None:
                raise ValueError("image encoding option does not apply to format")
        if any(getattr(self, name) is None for name in groups[self.format]):
            raise ValueError("image encoding options cannot be null")
        if self.format == "jpeg" and self.quality is not None and not 1 <= self.quality <= 95:
            raise ValueError("jpeg quality must be between 1 and 95")
        return self


class TimestampSampling(Parameters):
    mode: Literal["timestamps"] = "timestamps"
    timestamps_ms: list[Annotated[int, Field(ge=0)]] = Field(min_length=1)


class IntervalSampling(TimeRange):
    mode: Literal["interval"] = "interval"
    interval_ms: int = Field(ge=1)


class CountSampling(TimeRange):
    mode: Literal["count"] = "count"
    count: int = Field(ge=1)


class FramesParams(Parameters):
    video_stream: int = Field(default=0, ge=0, le=31)
    sampling: Annotated[TimestampSampling | IntervalSampling | CountSampling,
                        Field(discriminator="mode")] = Field(
                            default_factory=lambda: TimestampSampling(timestamps_ms=[0]))
    frame_selection: Literal["next", "previous", "nearest"] = "next"
    image: ImageParams = Field(default_factory=ImageParams)


class ProbeParams(Parameters):
    sections: list[Literal["format", "streams", "tags", "chapters"]] = Field(
        default_factory=lambda: ["format", "streams"], min_length=1, max_length=4)
    stream_indices: list[Annotated[int, Field(ge=0, le=31)]] | None = Field(default=None, max_length=32)
    count_frames: bool = False

    @model_validator(mode="after")
    def selections(self):
        self.sections = sorted(set(self.sections))
        if self.stream_indices is not None:
            self.stream_indices = sorted(set(self.stream_indices))
        if self.count_frames and "streams" not in self.sections:
            raise ValueError("count_frames requires streams section")
        return self
