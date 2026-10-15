import inspect
import re
from collections.abc import Callable
from fractions import Fraction
from typing import Annotated, Any, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, validate_default=True)


def parameter_description(model: type[BaseModel]) -> str:
    return inspect.cleandoc(model.__doc__ or "")


def dependent_default(selector: str, values: dict) -> Callable[[dict[str, Any]], None]:
    # 条件默认不能发布为无条件 default；null 也不能冒充省略字段后的实际值。
    def annotate(schema: dict[str, Any]) -> None:
        schema.pop("default", None)
        schema[f"x-default-by-{selector}"] = values
    return annotate


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
    start_ms: int = Field(default=0, ge=0, description="开始时间，毫秒；默认 0。", examples=[0, 1000])
    end_ms: int | None = Field(default=None, gt=0,
        description="结束时间，毫秒；省略/null 表示到媒体结束，必须大于 start_ms。", examples=[5000])

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class Resize(Parameters):
    width: int | None = Field(default=None, ge=1, le=65535,
        description="目标宽度，像素；只给宽度时等比计算高度，两边都省略则保持源尺寸；受部署限额约束。",
        examples=[1280])
    height: int | None = Field(default=None, ge=1, le=65535,
        description="目标高度，像素；只给高度时等比计算宽度；cover/pad 必须同时指定宽高。",
        examples=[720])
    fit: Literal["contain", "cover", "pad"] = Field(default="contain",
        description="contain 等比装入不补边（默认）；cover 填满并裁剪；pad 等比装入并补边。"
                    "cover/pad 需要宽高，视频目标框须为偶数尺寸。")
    allow_upscale: bool = Field(default=False,
        description="是否允许放大源画面，默认 false；无法满足目标框时可能拒绝处理。")
    background: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$",
        description="补边颜色，#RRGGBB；视频默认黑色。", examples=["#000000"])
    crop_x: float = Field(default=0.5, ge=0, le=1,
        description="cover 水平裁剪焦点：0 左、0.5 居中（默认）、1 右；其他 fit 只能使用默认值。")
    crop_y: float = Field(default=0.5, ge=0, le=1,
        description="cover 垂直裁剪焦点：0 上、0.5 居中（默认）、1 下；其他 fit 只能使用默认值。")

    @model_validator(mode="after")
    def geometry(self):
        if self.fit != "contain" and (self.width is None or self.height is None):
            raise ValueError("cover/pad require both dimensions")
        if self.fit != "cover" and (self.crop_x != 0.5 or self.crop_y != 0.5):
            raise ValueError("crop position requires cover")
        return self


class FrameRate(Parameters):
    mode: Literal["source", "constant", "max"] = Field(default="source",
        description="source 保留源帧时间（默认）；constant 固定帧率；max 只限制最高帧率。"
                    "仅给 value 时推导 constant。",
        json_schema_extra=dependent_default("value", {"omitted": "source", "provided": "constant"}))
    value: str | int | float | None = Field(default=None,
        description="帧/秒，可用 30 或 30000/1001；constant/max 必填正数，受部署 max_frame_rate 限制。"
                    "source 不允许非 null 值；字符串不接受指数记法。", examples=[30, "30000/1001"])

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
    mode: Literal["crf"] = Field(default="crf", description="按质量编码，与 bitrate 模式互斥。")
    value: float = Field(default=23.0, ge=0, le=63,
        description="CRF 越低通常越清晰；H.264/HEVC 0～51，VP9/AV1 为 0～63 整数。"
                    "本对象省略 value 为 23；整个 rate_control 省略时 VP9/AV1 使用 32。",
        examples=[23, 32])


class VideoBitrate(Parameters):
    mode: Literal["bitrate"] = Field(default="bitrate", description="按目标码率编码，与 crf 模式互斥。")
    bitrate_bps: int = Field(gt=0, description="目标视频码率，bit/s；例如 2 Mbps 写 2000000。",
        examples=[2000000])
    maxrate_bps: int | None = Field(default=None, gt=0,
        description="峰值码率，bit/s；与 buffer_size_bits 成对提供，不低于目标码率；AV1 不支持。",
        examples=[3000000])
    buffer_size_bits: int | None = Field(default=None, gt=0,
        description="码率缓冲区大小，bit；与 maxrate_bps 成对提供，AV1 不支持。", examples=[6000000])

    @model_validator(mode="after")
    def buffer(self):
        if (self.maxrate_bps is None) != (self.buffer_size_bits is None):
            raise ValueError("peak bitrate and buffer size must be specified together")
        if self.maxrate_bps is not None and self.maxrate_bps < self.bitrate_bps:
            raise ValueError("peak bitrate must not be below target bitrate")
        return self


class AudioBitrate(Parameters):
    mode: Literal["bitrate"] = Field(default="bitrate", description="AAC/MP3/Opus 显式目标码率模式。")
    bitrate_bps: int = Field(gt=0,
        description="音频码率，bit/s；128 kbps 写 128000。MP3 需符合采样率对应码率表；"
                    "AAC 不超过 6 × sample_rate × channels；详情查 capabilities.media_parameters。",
        examples=[128000])


class AudioQuality(Parameters):
    mode: Literal["quality"] = Field(default="quality", description="仅 AAC/MP3 支持质量模式。")
    value: float = Field(ge=0, le=9,
        description="AAC 0.1～5；MP3 0～9。两种编码的质量刻度不能直接比较。", examples=[2])


class AudioAutoBitrate(Parameters):
    mode: Literal["auto"] = Field(default="auto",
        description="默认有损音频码率策略，按编码器、实际采样率与声道数选择兼容目标码率。")


AudioMode = TypeVar("AudioMode", bound=str, default=Literal["encode", "copy", "drop"])


class AudioEncoding(Parameters, Generic[AudioMode]):
    mode: AudioMode = Field(default=cast(AudioMode, "encode"),
        description="encode 重新编码（默认）；copy 复制源音轨，禁止设置编码、采样率、声道、质量或裁剪。"
                    "仅视频内音轨另支持 drop 去掉音轨；copy 仍须源编码与目标容器兼容。")
    audio_stream: int = Field(default=0, ge=0, le=31,
        description="所选音轨在音频流中的序号，从 0 开始；默认第一条音轨，不是容器全局流索引。")
    codec: Literal["aac", "mp3", "opus", "flac", "pcm"] | None = Field(default="aac",
        description="encode 默认 aac；mp3/opus 为其他有损编码，flac 无损压缩，pcm 未压缩。"
                    "copy/drop 请省略。视频 WebM 音轨默认 opus；支持范围还需查 list_capabilities。",
        json_schema_extra=dependent_default("mode", {"encode": "aac", "copy": None}))
    sample_rate: Annotated[int, Field(ge=1, le=384000)] | Literal["auto", "source"] | None = Field(
        default="auto", description="采样率，Hz。auto 默认优先源值，不兼容则回落到 48000；source 严格保持。"
        "可传 44100/48000 等兼容整数；Opus 仅 48000，MP3/AAC 需符合能力接口的采样率表。copy/drop 省略。",
        examples=["auto", "source", 44100, 48000],
        json_schema_extra=dependent_default("mode", {"encode": "auto", "copy": None}))
    channels: Annotated[int, Field(ge=1, le=8)] | Literal["auto", "source"] | None = Field(default="auto",
        description="声道数：auto 默认优先源值，不兼容则回落到双声道；source 严格保持；整数为 1～8。"
                    "MP3 仅 1/2，Opus 仅 1/2/3/6/8；指定 channel_layout 时按布局推导。copy/drop 省略。",
        examples=["auto", "source", 1, 2],
        json_schema_extra=dependent_default("mode", {"encode": "auto", "copy": None}))
    channel_layout: Literal["mono", "stereo", "2.1", "3.0", "4.0", "5.1", "7.1"] | None = Field(
        default=None, description="声道布局，省略时由源与声道数推导。mono=1、stereo=2、2.1/3.0=3、"
        "4.0=4、5.1=6、7.1=8；显式 channels 必须一致。Opus 不支持 2.1/4.0。copy/drop 省略。")
    sample_format: Literal["s16", "s24", "f32"] | None = Field(default=None,
        description="仅 PCM：s16 为 16-bit 整数（默认），s24 为 24-bit 整数，f32 为 32-bit 浮点。"
                    "其他编码及 copy/drop 请省略。",
        json_schema_extra=dependent_default("codec", {"pcm": "s16"}))
    rate_control: Annotated[AudioBitrate | AudioQuality | AudioAutoBitrate,
                            Field(discriminator="mode")] | None = Field(default=None,
        description="AAC/MP3/Opus 默认 {mode:auto}，可改 bitrate；AAC/MP3 还可用 quality。"
                    "FLAC/PCM 和 copy/drop 请省略；整个对象替换默认策略。",
        examples=[{"mode": "auto"}, {"mode": "bitrate", "bitrate_bps": 128000}],
        json_schema_extra=dependent_default("codec", {
            "aac": {"mode": "auto"}, "mp3": {"mode": "auto"}, "opus": {"mode": "auto"},
            "flac": None, "pcm": None}))
    compression_level: int | None = Field(default=None, ge=0, le=12,
        description="仅 FLAC：压缩等级 0～12，默认 5，影响编码工作量和体积；当前输出固定 16-bit。"
                    "其他编码及 copy/drop 请省略。",
        json_schema_extra=dependent_default("codec", {"flac": 5}))

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
    """音频转换/音轨提取：params 省略时输出 AAC/M4A，采样率、声道和码率为 auto。

    codec 可选 aac/mp3/opus/flac/pcm，默认容器依次为 m4a/mp3/ogg/flac/wav；mka 支持全部五种。
    只改 codec 即推导容器，只改 container 不会推导 codec。Opus 固定 48 kHz；MP3 仅单/双声道。
    mode=copy 默认 mka，不能同时调整编码参数、使用预设或裁剪；源编码与容器仍需兼容。
    编码可用性和部署限额查询 HTTP GET /v1/capabilities 或 MCP list_capabilities。
    """

    model_config = ConfigDict(json_schema_extra={"examples": [
        {}, {"codec": "mp3"}, {"codec": "pcm", "sample_rate": 16000, "channels": 1},
        {"mode": "copy"}]})
    preset: Literal["aac_stereo_128k_v1", "wav_mono_16k_v1"] | None = Field(default=None,
        description="可选捷径，默认不用预设。aac_stereo_128k_v1 为 AAC/48 kHz/双声道/128 kbps；"
                    "wav_mono_16k_v1 为 PCM s16/16 kHz/单声道；显式设置覆盖预设，copy 禁用。")
    container: Literal["m4a", "mp3", "ogg", "flac", "wav", "mka"] = Field(default="m4a",
        description="省略时按 codec 推导：aac→m4a、mp3→mp3、opus→ogg、flac→flac、pcm→wav；"
                    "mka 兼容全部五种。mode=copy 默认 mka。仅改容器不会反向推导编码。",
        json_schema_extra=dependent_default("codec", {
            "aac": "m4a", "mp3": "mp3", "opus": "ogg", "flac": "flac", "pcm": "wav"}))
    range: TimeRange = Field(default_factory=TimeRange,
        description="截取时间范围，毫秒；默认处理完整音轨；copy 不支持裁剪。",
        json_schema_extra={"default": {"start_ms": 0, "end_ms": None}})

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
    """视频转换：params 省略时 H.264/MP4、CRF 23、medium，保持源尺寸与帧时间，音轨转 AAC。

    codec 可选 h264/hevc/vp9/av1；h264/hevc 默认 MP4/CRF23，vp9/av1 默认 WebM/CRF32/Opus。
    MP4 支持 h264/hevc/av1，WebM 支持 vp9/av1，MKV 支持四种；MP4 音轨仅 AAC/MP3，WebM 仅 Opus。
    可调整 resize、frame_rate、rate_control、audio、range；只改 codec 就推导兼容容器。
    当前仅软件编码、8-bit SDR；AV1 最小 64×64。编码可用性和限额查询 list_capabilities 或 GET /v1/capabilities。
    """

    model_config = ConfigDict(json_schema_extra={"examples": [
        {}, {"codec": "hevc"}, {"codec": "vp9"},
        {"resize": {"width": 1280}, "rate_control": {"mode": "crf", "value": 23}}]})
    preset: Literal["web_720p_v1"] | None = Field(default=None,
        description="默认不选预设；web_720p_v1 是 1280×720/30 fps 快捷设置，H.264/HEVC 默认配"
                    "48 kHz 双声道 AAC 128 kbps；显式参数覆盖预设。")
    container: Literal["mp4", "mkv", "webm"] = Field(default="mp4",
        description="MP4 支持 h264/hevc/av1，MKV 支持四种视频编码，WebM 仅 vp9/av1。"
                    "省略时 h264/hevc→mp4，vp9/av1→webm；仅改容器不会反推 codec。",
        json_schema_extra=dependent_default("codec", {
            "h264": "mp4", "hevc": "mp4", "vp9": "webm", "av1": "webm"}))
    codec: Literal["h264", "hevc", "vp9", "av1"] = Field(default="h264",
        description="软件视频编码：h264=H.264/AVC（默认）、hevc=H.265、vp9=VP9、av1=AV1。"
                    "填写公共名称，不使用 libx264 等 FFmpeg 实现名；可用性查询 capabilities。")
    video_stream: int = Field(default=0, ge=0, le=31,
        description="所选视频轨道在视频流中的序号，从 0 开始，默认第一条；不是容器全局流索引。")
    resize: Resize = Field(default_factory=Resize,
        description="默认保留尺寸、不放大；可单独设置宽或高，cover/pad 需给偶数宽高。",
        json_schema_extra={"default": {}})
    frame_rate: FrameRate = Field(default_factory=FrameRate,
        description="默认 source 保持源帧时间；constant 固定帧率，max 限制最高帧率。",
        json_schema_extra={"default": {"mode": "source", "value": None}})
    rate_control: Annotated[CRF | VideoBitrate, Field(discriminator="mode")] = Field(default_factory=CRF,
        description="省略整个对象时 h264/hevc 使用 CRF23，vp9/av1 使用 CRF32；也可指定 bitrate。"
                    "整个对象替换，不能混用 crf 与 bitrate；AV1 不支持峰值/缓冲参数。",
        examples=[{"mode": "crf", "value": 23}, {"mode": "bitrate", "bitrate_bps": 2000000}],
        json_schema_extra=dependent_default("codec", {
            codec: {"mode": "crf", "value": 23 if codec in ("h264", "hevc") else 32}
            for codec in VIDEO_ENCODERS}))
    speed: Literal["fast", "medium", "slow"] = Field(default="medium",
        description="编码速度/压缩效率取舍：fast 快、medium 均衡（默认）、slow 慢；不改变播放速度。")
    pixel_format: Literal["yuv420p"] = Field(default="yuv420p",
        description="当前仅支持 8-bit SDR yuv420p，不支持 10-bit/HDR 或硬件编码参数。")
    audio: AudioEncoding = Field(default_factory=lambda: AudioEncoding.model_validate({}),
        description="视频音轨：encode（默认）、copy、drop；无源音轨时输出无音轨视频。"
                    "MP4 默认 AAC、仅兼容 AAC/MP3；WebM 默认且仅兼容 Opus；MKV 默认 AAC、兼容五种编码。",
        json_schema_extra=dependent_default("container", {
            container: AudioEncoding.model_validate({"codec": "opus" if container == "webm" else "aac"}).model_dump()
            for container in VIDEO_CONTAINERS}))
    range: TimeRange = Field(default_factory=TimeRange,
        description="截取时间范围，毫秒；默认处理完整视频；audio.mode=copy 不支持裁剪。",
        json_schema_extra={"default": {"start_ms": 0, "end_ms": None}})

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
    """图片缩放/格式转换：params 省略时保持尺寸，输出 JPEG quality 90，不放大。

    format 可选 jpeg（不能写 jpg）/png/webp。JPEG quality 1～95 默认90；WebP 0～100 默认85；
    PNG compression_level 0～9 默认6，不接受 quality。只改 format 会选相应默认参数。
    fit 可选 contain/cover/pad；cover/pad 需宽高。JPEG 用白色背景合成透明区域，PNG/WebP 可保留透明。
    当前仅静态 8-bit SDR；尺寸与输出限额查询 list_capabilities 或 GET /v1/capabilities。
    """

    model_config = ConfigDict(json_schema_extra={"examples": [
        {}, {"format": "png"}, {"format": "webp", "quality": 85},
        {"width": 320, "height": 240, "fit": "pad"}]})
    background: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$",
        description="补边及透明转 JPEG 的背景色，#RRGGBB；图片默认白色。", examples=["#ffffff"])
    resampling: Literal["lanczos", "bicubic", "bilinear", "nearest"] = Field(default="lanczos",
        description="缩放算法：lanczos（默认）、bicubic 双三次、bilinear 双线性、nearest 最近邻；"
                    "nearest 可保留像素块边界。")
    format: Literal["jpeg", "png", "webp"] = Field(default="jpeg",
        description="图片输出格式：jpeg（默认；不能写 jpg）、png、webp；使用 Pillow 编码。"
                    "只设置 format 默认保留尺寸，不能同时传其他格式的专属选项。")
    quality: int | None = Field(default=None, ge=0, le=100,
        description="JPEG 1～95 默认90，WebP 0～100 默认85；PNG 请省略。适用格式下不能显式传 null。"
                    "有损模式越高通常越清晰；WebP lossless 时控制压缩工作量。", examples=[85, 90],
        json_schema_extra=dependent_default("format", {"jpeg": 90, "webp": 85, "png": None}))
    compression_level: int | None = Field(default=None, ge=0, le=9,
        description="仅 PNG：压缩等级 0～9，默认6，影响工作量和体积；不是有损画质，不能传 null。",
        json_schema_extra=dependent_default("format", {"png": 6, "jpeg": None, "webp": None}))
    jpeg_subsampling: Literal[0, 1, 2] | None = Field(default=None,
        description="仅 JPEG：0=4:4:4、1=4:2:2、2=4:2:0（默认）；不能显式传 null。",
        json_schema_extra=dependent_default("format", {"jpeg": 2, "png": None, "webp": None}))
    progressive: bool | None = Field(default=None,
        description="仅 JPEG：是否渐进式输出，默认 false；不能显式传 null。",
        json_schema_extra=dependent_default("format", {"jpeg": False, "png": None, "webp": None}))
    lossless: bool | None = Field(default=None,
        description="仅 WebP：是否无损编码，默认 false；不能显式传 null。",
        json_schema_extra=dependent_default("format", {"webp": False, "jpeg": None, "png": None}))
    method: int | None = Field(default=None, ge=0, le=6,
        description="仅 WebP：编码工作量档位 0～6，默认4；越高通常越慢；不能显式传 null。",
        json_schema_extra=dependent_default("format", {"webp": 4, "jpeg": None, "png": None}))

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
    mode: Literal["timestamps"] = Field(default="timestamps", description="按指定时间点逐项抽帧。")
    timestamps_ms: list[Annotated[int, Field(ge=0)]] = Field(min_length=1,
        description="时间点列表，毫秒；视频首张可解码帧为 0；保留顺序与重复项，越界逐项报错。"
                    "总数受部署 max_frame_count 限制。", examples=[[0, 1000, 2000]])


class IntervalSampling(TimeRange):
    mode: Literal["interval"] = Field(default="interval", description="在 start_ms 到 end_ms 内按间隔抽帧。")
    interval_ms: int = Field(ge=1, description="抽帧间隔，毫秒；从开始点生成到不超过结束点，受帧数限额约束。",
        examples=[1000])


class CountSampling(TimeRange):
    mode: Literal["count"] = Field(default="count", description="在指定区间均匀取样，包含首末端点。")
    count: int = Field(ge=1, description="请求帧数；1 表示只取开始点；受部署 max_frame_count 限制，"
        "失败项不自动补齐。", examples=[3])


class FramesParams(Parameters):
    """视频抽帧：params 省略时取 0ms 首帧，保留尺寸，输出 JPEG quality90 集合。

    sampling.mode 可选 timestamps 指定毫秒列表、interval 按间隔、count 均匀取指定数量。
    frame_selection 可选 next（默认）/previous/nearest；image 复用完整图片参数，format 为 jpeg/png/webp，
    默认质量分别 JPEG90/WebP85，PNG 压缩等级6。越界逐项报错，不补齐失败帧。
    抽帧数量、尺寸和输出限额查询 list_capabilities 或 GET /v1/capabilities。
    """

    model_config = ConfigDict(json_schema_extra={"examples": [
        {}, {"sampling": {"mode": "count", "count": 3}, "image": {"width": 320, "format": "webp"}},
        {"sampling": {"mode": "timestamps", "timestamps_ms": [0, 1000]}}]})
    video_stream: int = Field(default=0, ge=0, le=31,
        description="所选视频轨道在视频流中的序号，从 0 开始，默认第一条；不是容器全局流索引。")
    sampling: Annotated[TimestampSampling | IntervalSampling | CountSampling,
                        Field(discriminator="mode")] = Field(
        default_factory=lambda: TimestampSampling(timestamps_ms=[0]),
        description="timestamps 指定时间点，interval 按间隔，count 按数量均匀取样；默认取 0ms 首帧。"
                    "各模式互斥，范围以首张可解码帧归零，省略结束点表示最后可解码帧。",
        json_schema_extra={"default": {"mode": "timestamps", "timestamps_ms": [0]}})
    frame_selection: Literal["next", "previous", "nearest"] = Field(default="next",
        description="next 选不早于目标点的帧（默认）；previous 不晚于；nearest 最近，平局选之前。")
    image: ImageParams = Field(default_factory=ImageParams,
        description="复用图片转换参数；默认保持尺寸、JPEG quality90；可改 png/webp 或宽高、质量。",
        json_schema_extra={"default": {}})


class ProbeParams(Parameters):
    """媒体探测：params 省略时返回 format 文件概况和 streams 轨道信息，不扫描帧数。

    sections 可选 format/streams/tags/chapters；tags 仅返回受控的 language、creation_time。
    stream_indices 可选择容器全局流索引，省略表示全部；count_frames=true 扫描统计帧数，必须包含 streams。
    输出为 JSON 报告；扫描受任务时间与资源限额约束，限额查询 list_capabilities 或 GET /v1/capabilities。
    """

    model_config = ConfigDict(json_schema_extra={"examples": [
        {}, {"sections": ["format", "streams", "chapters"]},
        {"sections": ["streams"], "count_frames": True}]})
    sections: list[Literal["format", "streams", "tags", "chapters"]] = Field(
        default_factory=lambda: ["format", "streams"], min_length=1, max_length=4,
        description="报告分区：format 文件概况、streams 轨道、tags 受控标签、chapters 章节；"
                    "默认 format/streams，tags 仅允许 language/creation_time。",
        json_schema_extra={"default": ["format", "streams"]})
    stream_indices: list[Annotated[int, Field(ge=0, le=31)]] | None = Field(default=None, max_length=32,
        description="容器全局流索引列表；默认省略/null 表示全部允许的流；未知索引报错。", examples=[[0]])
    count_frames: bool = Field(default=False,
        description="是否实际扫描统计帧数，默认 false；true 时 sections 必须包含 streams，计入执行时限。")

    @model_validator(mode="after")
    def selections(self):
        self.sections = sorted(set(self.sections))
        if self.stream_indices is not None:
            self.stream_indices = sorted(set(self.stream_indices))
        if self.count_frames and "streams" not in self.sections:
            raise ValueError("count_frames requires streams section")
        return self
