# -*- coding: utf-8 -*-
"""大音频分块脚本（按大小从后往前填满每块 → 跟随输入的 CBR/VBR/ABR + 32 kbps 前导静音）

流程：
  1. 原音频 → 内存里的 PCM（ffmpeg 管道解码一次，不落 WAV）
  2. 默认按大小切块：从最后一块往前逐块试编码，让每块「前导静音 + 音乐」编码后
     不超过 MAX_CHUNK_BYTES（默认 8 MiB）；最后几块填到尽量满，第一块是剩下的。
     想按固定时长切就用 -s 60（旧行为）。
  3. 每块单独编码成 MP3，编码方式跟着输入走：
       MP3 输入 → 它自己用什么方式就用什么方式（CBR / ABR / VBR）：CBR、ABR 用
                  它的码率，VBR 取和它平均码率最接近的 V0~V9 质量档；
       非 MP3   → VBR（V0，约 245 kbps）。
     手动 -m 指定的方式跟输入不一样时（或者输入根本不是 MP3），按最高档编：
       CBR / ABR 用 320 kbps，VBR 用 VBR_QUALITY（默认 0 = 最高）。
     比特池保持开启（-reservoir 1，LAME 默认），码率分配更从容。
  4. 只有 CBR 替换静音帧：把前导静音里"无论怎样都肯定全在静音里"的帧，换成预生成的
     32 kbps 静音帧（只是替换帧，帧数不变，所以解码后的采样数和音乐起点分毫不变）。
     VBR/ABR 不替换：LAME 本来就把数字静音编成 32 kbps 的帧，体积已经一样了。
  5. 如果 CBR/ABR 编完放不下（分块失败，或有块超过体积上限），就整首改用 VBR 重来一次
     （VBR 质量按原来那个目标的平均码率换算）。
  6. 回读校验：采样数、静音段是否全零、音乐段和源 PCM 的误差

注意：按大小切块后块数会变（不再固定 8 块），.sb3 里的声音数量要一起改。

过程不生成 temp 目录：PCM 和静音帧都在内存里；反着填时先写工作文件，
算完块数再改名成正式块。

为什么音质能和 input.mp3 一样：
      MP3 是有损格式，重新编码不可能逐字节无损；真无损只能用 WAV/FLAC，
      但 60 秒立体声 WAV 就超过 10 MiB，会超过 8 MiB 的上传限制。
      音乐段跟着输入的编码方式走：
        输入 CBR 320k   → 输出 CBR 320k（约 39–41 dB SNR）
        输入 ABR 192k   → 输出 ABR 192k
        输入 VBR V2     → 输出 VBR V2
      128k 输入 → 输出 128k 左右（约 22 dB，会再多一点损失）。想尽量保音质、
      不在乎体积时，可以用 -b 320k 强制按 320k 编码（VBR 输入会按 V0 编）。

为什么静音可以几乎不占体积：
      静音区全是数字静音，编码出来的帧本来就不借前文（main_data_begin == 0），
      所以可以在音乐开始之前找一个不借前文的帧当分界，把它之前的帧整段换成
      32 kbps 的静音帧：帧数不变 → 解码出来的采样数、音乐起点都不变，
      静音部分固定只按 32 kbps 算（每分钟约 0.23 MiB）。
      分界点之后保留的帧是原样字节，只要每一帧借的字节都还落在保留区内部，
      音乐段就逐位不变；make_spliced() 会逐帧校验这一点。
      VBR/ABR 不用这一步：LAME 在静音处本来就会掉到最低的 32 kbps，
      换不换体积一样，所以只有 CBR 才替换。

为什么这样能无缝：
      每块都是从 PCM 独立编码出的新文件，解码器从它的第一帧开始解（第一帧的
      main_data_begin 一定是 0），文件内部怎么借字节都不影响别的块；编码器把
      延迟/填充写进了 LAME 标签，解码器会自动裁掉，所以每块解码出来就是精确的 PCM。
      （ffmpeg 写这个标签时偶尔会把填充算少几十个采样，脚本编码完会重算并改准。）
      替换静音帧只发生在音乐开始之前（后面还留了至少 2 帧原编码的静音），
      解码器在那里的状态本来就是全零，因此音乐部分逐采样不变。

用法：
      python split.py
      python split.py --help
      python split.py -s 60 -i 另一首.mp3 -o out
      python split.py --max-size 4M
      python split.py -m vbr            # 不管输入是什么，统一用 VBR
      python split.py -m cbr -b 192k    # 不管输入是什么，统一用 CBR 192k
"""

import argparse
import array
import json
import math
import os
import re
import shutil
import subprocess
import sys

# ============ 配置区（默认值，可被命令行参数覆盖） ============
SRC = "input.mp3"
OUT_DIR = "output-chunks"
NAME_FMT = "{0}.mp3"      # {0} 是块序号，从 1 开始（和 Scratch 里默认的声音编号一致）
# 每块时长的 JSON 数组（给 Scratch 列表导入用），相对脚本目录。
DURATION_FILE = "output-chunks-duration.json"

# 默认按大小切块：从后往前把每块填满，但不超过 MAX_CHUNK_BYTES。
MAX_CHUNK_BYTES = 8 * 1024 * 1024
# 想按固定时长切块（旧行为）就把这里设成秒数（例如 60），或者用 -s 60。
SECONDS_PER_CHUNK = None

# 编码方式："auto" = 跟着输入走（MP3 用它的 CBR/ABR/VBR，其它格式用 VBR）。
# 也可以直接写 "cbr" / "abr" / "vbr" 强制指定。
MUSIC_MODE = "auto"
# 目标码率："auto" = 跟着输入走（MP3 用它自己的码率，其它格式用 320k）。
# CBR/ABR 直接拿它当码率；VBR 用显式指定的码率挑最接近的 V0~V9 质量档。
# 也可以直接写 "320k" / "256k" / "192k" 强制指定。
MUSIC_BITRATE = "auto"
# 手动指定 -m vbr 时用的质量档 0~9：0 最高（约 245 kbps），9 最低（约 65 kbps）。
# 输入本来就是 VBR、又没换方式时不用这个，仍按它的平均码率换算质量档。
VBR_QUALITY = 0
# 前导静音码率（只有 CBR 替换静音帧时用得到）：MPEG-1 立体声允许的最低码率，
# 只影响体积，不影响音质。
SILENCE_BITRATE = "32k"
# ============================================================

SR = 44100
CH = 2
SAMPWIDTH = 2
FRAME_BYTES = CH * SAMPWIDTH

# MPEG-1 Layer III：每帧固定 1152 个采样
MP3_SAMPLES = 1152
BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
SAMPLE_RATES = [44100, 48000, 32000]
SIZE_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
MODE_NAMES = {"cbr": "CBR", "abr": "ABR", "vbr": "VBR"}

# LAME 的 Xing 标签里记的编码方式（VbrTag.c 的注释）
LAME_VBR_METHODS = {1: "cbr", 2: "abr", 3: "vbr", 4: "vbr",
                    5: "vbr", 6: "vbr", 7: "vbr", 8: "cbr", 9: "abr"}
# LAME V0~V9 的典型平均码率（kbps），用来把「目标码率」换算成质量档
VBR_QUALITY_KBPS = {0: 245, 1: 225, 2: 190, 3: 175, 4: 165,
                    5: 130, 6: 115, 7: 100, 8: 85, 9: 65}


class FitError(RuntimeError):
    """放不下：分块失败，或者某一块超过体积上限。"""


def decode_bytes(path):
    """把音频解码成 s16le / 44.1 kHz / 立体声 的原始字节。"""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0",
                        "-f", "s16le", "-ac", str(CH), "-ar", str(SR), "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("解码失败: %s\n%s" % (path, p.stderr.decode("utf-8", "replace")))
    return p.stdout[: len(p.stdout) // 2 * 2]


def iter_mp3_frames(data):
    """逐帧解析 MPEG-1 Layer III，产出 dict(off, size, br, sr, mdb, xing)。

    off/size  帧在文件里的位置和长度
    br        该帧码率（kbps）
    sr        采样率
    mdb       main_data_begin，0 表示这一帧不引用前面的字节（自包含）
    xing      Xing/Info 标签帧（元数据帧，不是音频）
    """
    off = 0
    n = len(data)
    if data[:3] == b"ID3" and n >= 10:
        sz = ((data[6] & 0x7F) << 21) | ((data[7] & 0x7F) << 14) | \
             ((data[8] & 0x7F) << 7) | (data[9] & 0x7F)
        off = 10 + sz
    while off + 4 <= n:
        if data[off] != 0xFF or (data[off + 1] & 0xE0) != 0xE0:
            off += 1
            continue
        ver = (data[off + 1] >> 3) & 3
        layer = (data[off + 1] >> 1) & 3
        br_i = (data[off + 2] >> 4) & 0xF
        sr_i = (data[off + 2] >> 2) & 3
        if ver != 3 or layer != 1 or br_i in (0, 15) or sr_i == 3:
            off += 1
            continue
        sr = SAMPLE_RATES[sr_i]
        size = 144000 * BITRATES[br_i] // sr + ((data[off + 2] >> 1) & 1)
        if off + size > n:
            break
        mode = (data[off + 3] >> 6) & 3
        mdb = (data[off + 4] << 1) | (data[off + 5] >> 7)
        tag_at = off + 4 + (17 if mode == 3 else 32) + (2 if (data[off + 1] & 1) == 0 else 0)
        xing = data[tag_at:tag_at + 4] if data[tag_at:tag_at + 4] in (b"Xing", b"Info") else None
        yield dict(off=off, size=size, br=BITRATES[br_i], sr=sr, mdb=mdb, xing=xing,
                   tag_at=tag_at)
        off += size


def mp3_frames(data):
    """拆出 MPEG-1 Layer III 的帧，返回列表。"""
    return list(iter_mp3_frames(data))


def lame_tag(data, frame):
    """读 Xing/Info 帧里附带的 LAME 标签，返回 (编码器名, VBR 方式码)。

    没有标签、或者标签不是 LAME 写的时候返回 (None, None)：ffmpeg 自己写的标签
    （"Lavc…"）方式码永远是 0，看不出编码方式，只能靠帧码率猜。
    """
    pos = lame_tag_at(data, frame, 10)
    if pos is None:
        return None, None
    enc = data[pos:pos + 9]
    if enc[:4] not in (b"LAME", b"Lavf", b"Lavc"):
        return None, None
    return enc.decode("ascii", "replace").strip(), data[pos + 9] & 0x0F


def mp3_info(data):
    """扫一遍 MP3，判断它用的是 CBR、ABR 还是 VBR。

    判断顺序（越靠前越可信）：
      1. LAME 标签里写了方式就用它（LAME 自己编的文件都有）
      2. 所有音频帧码率都一样（或 99.5% 一样）→ CBR
      3. Xing 标签帧的码率正好是目标码率、平均码率也贴着它 → ABR
         （ffmpeg 编 ABR 时，标签帧按目标码率写；VBR 的标签帧固定是 64 kbps）
      4. 剩下都算 VBR
    不是 MP3（或者不是 MPEG-1 Layer III）时返回 None。
    """
    frames = mp3_frames(data)
    if not frames:
        return None
    tag_frame = frames[0] if frames[0]["xing"] else None
    audio = frames[1:] if tag_frame else frames
    if not audio:
        return None

    count = len(audio)
    total = sum(f["size"] for f in audio)
    sr = audio[0]["sr"]
    low = min(f["br"] for f in audio)
    high = max(f["br"] for f in audio)
    avg = total * 8 * sr / (count * MP3_SAMPLES) / 1000.0
    hist = {}
    for f in audio:
        hist[f["br"]] = hist.get(f["br"], 0) + 1
    share = max(hist.values()) / float(count)

    encoder, method = lame_tag(data, tag_frame)
    tag = tag_frame["xing"].decode("ascii") if tag_frame else None
    tag_br = tag_frame["br"] if tag_frame else None

    if method in LAME_VBR_METHODS:
        mode = LAME_VBR_METHODS[method]
        why = "LAME 标签写着 %s，平均 %.0f kbps" % (MODE_NAMES[mode], avg)
    elif share >= 0.995:
        mode = "cbr"
        why = "每帧都是 %d kbps" % max(hist, key=hist.get)
    elif tag and tag_br and tag_br in BITRATES[1:] and \
            abs(avg - tag_br) <= max(4.0, tag_br * 0.05):
        mode = "abr"
        why = "标签帧按 %d kbps 写，平均 %.0f kbps" % (tag_br, avg)
    else:
        mode = "vbr"
        why = "帧码率 %d~%d kbps，平均 %.0f kbps" % (low, high, avg)

    # 帧要覆盖住整个文件才算真的是 MP3，免得把别的格式误认成 MP3
    span = len(data) - frames[0]["off"]
    covered = sum(f["size"] for f in frames)
    return dict(mode=mode, why=why, avg=avg, low=low, high=high, sr=sr, count=count,
                tag=tag, tag_br=tag_br, encoder=encoder, method=method,
                coverage=(covered / float(span)) if span > 0 else 0.0)


def probe_codec(path):
    """用 ffprobe 看输入文件的音频编码格式和码率；失败时返回 (None, None)。"""
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "stream=codec_name,codec_type,bit_rate",
             "-show_entries", "format=bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    if p.returncode != 0:
        return None, None
    try:
        info = json.loads(p.stdout)
    except ValueError:
        return None, None
    audio = [s for s in (info.get("streams") or []) if s.get("codec_type") == "audio"]
    if not audio:
        return None, None
    codec = audio[0].get("codec_name")
    br = audio[0].get("bit_rate") or (info.get("format") or {}).get("bit_rate")
    try:
        br = int(br)
    except (TypeError, ValueError):
        br = None
    return codec, br


def nearest_bitrate(kbps):
    """取最接近的标准 MP3 CBR 码率（32~320 kbps）。"""
    best = BITRATES[1]
    for b in BITRATES[1:]:
        if abs(b - kbps) < abs(best - kbps):
            best = b
    return best


def quality_for_kbps(kbps):
    """按目标平均码率挑最接近的 VBR 质量档（0~9）。"""
    return min(VBR_QUALITY_KBPS, key=lambda q: abs(VBR_QUALITY_KBPS[q] - kbps))


def make_plan(mode, kbps=None, q=None, note=""):
    """打包一份编码方案。

    mode  cbr / abr / vbr
    kbps  cbr、abr 的目标码率
    q     vbr 的质量档 0~9（不填就用 VBR_QUALITY，默认 0 = 最高）
    note  给人看的说明（这份方案是怎么来的）
    """
    if mode == "vbr":
        q = VBR_QUALITY if q is None else int(q)
        return dict(mode=mode, kbps=None, q=q,
                    est_kbps=VBR_QUALITY_KBPS.get(q, 190), note=note)
    kbps = nearest_bitrate(320 if kbps is None else kbps)
    return dict(mode=mode, kbps=kbps, q=None, est_kbps=kbps, note=note)


def plan_label(plan):
    """方案的短名字，例如 "CBR 320 kbps"、"VBR V2（约 190 kbps）"。"""
    if plan["mode"] == "vbr":
        return "VBR V%d（约 %d kbps）" % (plan["q"], plan["est_kbps"])
    return "%s %d kbps" % (MODE_NAMES[plan["mode"]], plan["kbps"])


def with_mode(plan, mode, note):
    """换一种编码方式，尽量保住原来的码率/音质（放不下时自动改用 VBR 用）。"""
    if mode == "vbr":
        return make_plan("vbr", q=quality_for_kbps(plan["est_kbps"]), note=note)
    return make_plan(mode, kbps=plan["est_kbps"], note=note)


def choose_encoding(src, mode_arg, bitrate_arg):
    """按命令行参数和输入文件决定编码方式，返回 plan。

    输入是 MP3 时跟着它的 CBR/ABR/VBR 走；不是 MP3 时默认 VBR。
    -m 手动指定方式，-b 手动指定目标码率（VBR 时换算成质量档）。
    手动指定的方式跟输入不一样时（或者输入根本不是 MP3），按最高档编：
    CBR/ABR 用 320 kbps，VBR 用 VBR_QUALITY（默认 0 = 最高）。
    """
    forced = parse_kbps(bitrate_arg)
    codec, _ = probe_codec(src)

    info = None
    # ffprobe 已经说了不是 MP3 就别去逐帧扫了（别的格式扫一遍也很慢）
    if codec in (None, "mp3"):
        try:
            with open(src, "rb") as fh:
                info = mp3_info(fh.read())
        except OSError:
            info = None
        # 扫出来不像 MP3（帧没盖住整个文件）就不当成 MP3
        if info is not None and info["coverage"] < 0.95:
            info = None
    is_mp3 = info is not None and codec in (None, "mp3")

    if is_mp3:
        detected = info["mode"]
        detected_kbps = info["avg"]
        note = "输入是 MP3，跟随它的 %s（%s）" % (MODE_NAMES[detected], info["why"])
        if info["encoder"]:
            note += "，编码器 %s" % info["encoder"]
    else:
        detected = "vbr"
        detected_kbps = 320
        note = "输入不是 MP3（%s）" % (codec or "读不出格式")

    mode = detected if mode_arg == "auto" else mode_arg
    manual = mode_arg != "auto"
    # 手动指定的方式跟输入不一样 = 换了方式，没给 -b 就按最高档编
    switched = manual and mode_arg != detected
    if forced is not None:
        target = forced
    elif switched:
        target = BITRATES[-1]          # MP3 能给的最高码率 320 kbps
    else:
        target = detected_kbps
    # VBR：换了方式就用 VBR_QUALITY（默认最高档），否则按目标平均码率换算
    q = VBR_QUALITY if switched else quality_for_kbps(target)
    plan = make_plan(mode, kbps=target, q=q, note=note)

    if switched:
        plan["note"] += "，手动指定了 %s" % MODE_NAMES[mode]
        if not is_mp3:
            plan["note"] += "（输入不是 MP3）"
        else:
            plan["note"] += "（输入是 %s）" % MODE_NAMES[detected]
        if forced is None:
            plan["note"] += "，按最高档 %s" % (
                "V%d" % plan["q"] if plan["mode"] == "vbr" else "%d kbps" % plan["kbps"])
    if forced is not None:
        plan["note"] += "，码率按命令行指定"
    elif not manual and not is_mp3:
        plan["note"] += "，默认 %s" % plan_label(plan)
    return plan


def decode_mp3_data(data):
    """把 MP3 字节从管道解码成 s16le / 44.1 kHz / 立体声 的原始字节。"""
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:0",
                        "-f", "s16le", "-ac", str(CH), "-ar", str(SR), "-"],
                       input=data, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("解码失败: " + p.stderr.decode("utf-8", "replace"))
    return p.stdout[: len(p.stdout) // 2 * 2]


def make_silence_frame(bitrate):
    """在内存里生成一帧低码率数字静音，用来替换前面几分钟的静音帧（只有 CBR 用）。"""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "anullsrc=r=%d:cl=stereo" % SR,
         "-t", "0.2", "-c:a", "libmp3lame", "-b:a", bitrate,
         "-reservoir", "0", "-ar", str(SR), "-ac", str(CH),
         "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0",
         "-f", "mp3", "pipe:1"],
        capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("生成静音帧失败: " + p.stderr.decode("utf-8", "replace"))
    data = p.stdout
    audio = [f for f in mp3_frames(data) if not f["xing"]]
    if not audio:
        raise RuntimeError("没能生成静音帧")
    if any(f["mdb"] != 0 for f in audio):
        raise RuntimeError("静音帧没有关掉比特池，不能独立解码")
    if any(decode_mp3_data(data)):
        raise RuntimeError("静音帧解码结果不是全零")
    f = audio[0]
    return data[f["off"]:f["off"] + f["size"]]


def padded_pcm(pcm, start, length):
    """返回 [start 个静音] + [pcm 从 start 开始的 length 个采样] 的 s16le 字节。"""
    zeros = b"\x00" * (start * FRAME_BYTES)
    music = pcm[start * CH:(start + length) * CH].tobytes()
    return zeros + music


def plan_args(plan):
    """把方案变成 libmp3lame 的编码参数。"""
    if plan["mode"] == "cbr":
        return ["-b:a", "%dk" % plan["kbps"]]
    if plan["mode"] == "abr":
        return ["-b:a", "%dk" % plan["kbps"], "-abr", "1"]
    return ["-q:a", str(plan["q"])]


def encode_pcm(data, dst, plan):
    """把 s16le PCM 从管道喂给 ffmpeg，按方案编码成 MP3（保留比特池）。

    dst 为 None 时写到标准输出并返回字节，否则写进文件、返回 None。
    """
    p = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "-i", "pipe:0",
         "-map", "0:a:0", "-c:a", "libmp3lame"] + plan_args(plan) +
        ["-reservoir", "1", "-ar", str(SR), "-ac", str(CH),
         "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0",
         "-f", "mp3", "pipe:1" if dst is None else dst],
        input=data, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("编码失败: %s\n%s"
                          % (dst or "<管道>", p.stderr.decode("utf-8", "replace")))
    return p.stdout if dst is None else None


def prepare_silence(plan, silence_bitrate):
    """准备静音基准：返回 (CBR 用的静音帧, 静音每采样字节数)。

    只有 CBR 要把静音帧换掉，所以只有它需要 donor；VBR/ABR 也量一下静音有多
    占地方（LAME 本来就把数字静音编成 32 kbps），用来按体积切块。
    """
    if plan["mode"] == "cbr":
        donor = make_silence_frame(silence_bitrate)
        return donor, len(donor) / float(MP3_SAMPLES)
    data = encode_pcm(b"\x00" * (SR // 5 * FRAME_BYTES), None, plan)
    frames = [f for f in mp3_frames(data) if not f["xing"]]
    if not frames:
        raise RuntimeError("量不出静音体积：静音编码结果里没有帧")
    return None, sum(f["size"] for f in frames) / float(len(frames) * MP3_SAMPLES)


def main_data_bytes(data, f):
    """一帧的主数据区字节数（帧头 4 + MPEG-1 立体声侧信息 32 + 可能的 CRC）。"""
    return f["size"] - 4 - 32 - (0 if data[f["off"] + 1] & 1 else 2)


def splice_safe(data, audio, keep):
    """从第 keep 帧开始保留是否安全：比特池引用全都落在保留区内部。

    保留区是原样拷贝的字节，所以只要每一帧借的字节都还在保留区里，解码出的
    音乐就和没替换静音帧时逐位相同。第一帧还必须 main_data_begin == 0，
    否则它借的是被替换掉的那段字节。
    每帧最多借 511 字节，保留区攒够 511 字节后，后面所有帧都肯定安全。
    """
    if not 0 <= keep < len(audio) or audio[keep]["mdb"] != 0:
        return False
    avail = 0
    for f in audio[keep:]:
        if f["mdb"] > avail:
            return False
        avail += main_data_bytes(data, f)
        if avail > 511:
            break
    return True


def make_spliced(data, lead, donor):
    """把前导静音里整帧都在静音区的帧换成低码率静音帧，返回 (字节, 替换帧数)。

    分界点从「音乐开始前一帧」往前找，直到找到一个不借前文的帧（静音区里
    这种帧到处都是），并逐帧确认保留区的比特池引用都在保留区内部。这样替换
    掉的只有静音，音乐段和原编码逐位相同；找不到安全的分界点就直接报错。
    """
    frames = mp3_frames(data)
    if not frames or not frames[0]["xing"]:
        raise RuntimeError("编码结果里没有 Xing/Info 帧（无法可靠对齐）")
    audio = frames[1:]
    if not audio:
        raise RuntimeError("编码结果里没有音频帧")

    # 只替换「就算编码器延迟到 1 帧，也肯定整帧都在静音里」的帧：
    # 末尾至少留 1 帧整帧静音 + 音乐开始的那一帧，保证音乐起点不受影响。
    keep = min(max(0, lead // MP3_SAMPLES - 1), len(audio) - 1)
    while keep > 0 and not splice_safe(data, audio, keep):
        keep -= 1
    if not splice_safe(data, audio, keep):
        raise RuntimeError("找不到不借前文的分界帧，替换静音帧会破坏音乐")

    tag = data[frames[0]["off"]:frames[0]["off"] + frames[0]["size"]]
    body = b"".join(data[f["off"]:f["off"] + f["size"]] for f in audio[keep:])
    return tag + donor * keep + body, keep


def lame_tag_at(data, frame, need=36):
    """Xing/Info 帧里 LAME 标签的位置；帧里放不下就返回 None。"""
    if not frame or not frame["xing"]:
        return None
    t = frame["tag_at"]
    flags = int.from_bytes(data[t + 4:t + 8], "big")
    pos = t + 8
    pos += 4 if flags & 0x01 else 0       # 帧数
    pos += 4 if flags & 0x02 else 0       # 字节数
    pos += 100 if flags & 0x04 else 0     # TOC
    pos += 4 if flags & 0x08 else 0       # 质量
    if pos + need > frame["off"] + frame["size"]:
        return None
    return pos


def crc16_arc(data):
    """LAME 标签用的 CRC-16（多项式 0x8005，反射）。"""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def retime_tag(data, expected):
    """改 LAME 标签里的「编码延迟 + 填充」，让解码出来正好 expected 个采样。

    LAME 的模型是：解码结果 = 帧数 × 1152，前 delay 个采样是编码器热身、最后
    pad 个采样是填充，中间这一段才是音乐。所以 pad = 帧数 × 1152 - delay - 长度。
    ffmpeg 自己写的标签在收尾多出一帧时会把 pad 算少几十个采样，不修的话每块
    解码出来都会比源多几十个采样，接缝处对不齐，校验也会判 BAD。
    只动标签里的 3 个字节（和标签 CRC），音频数据一个字节都不碰。
    """
    frames = mp3_frames(data)
    if not frames or not frames[0]["xing"]:
        raise RuntimeError("编码结果里没有 Xing/Info 帧（没法对齐长度）")
    audio = frames[1:]
    if not audio:
        raise RuntimeError("编码结果里没有音频帧")
    pos = lame_tag_at(data, frames[0])
    if pos is None:
        raise RuntimeError("Xing 帧里放不下 LAME 标签（没法对齐长度）")

    delay = (data[pos + 21] << 4) | (data[pos + 22] >> 4)
    pad = len(audio) * MP3_SAMPLES - delay - expected
    if not 0 <= pad < 4096:
        raise RuntimeError("填充长度算出来是 %d，超出 LAME 标签能表示的范围" % pad)

    out = bytearray(data)
    out[pos + 21] = (delay >> 4) & 0xFF
    out[pos + 22] = ((delay & 0x0F) << 4) | ((pad >> 8) & 0x0F)
    out[pos + 23] = pad & 0xFF
    # 标签 CRC 覆盖标签帧的前 190 个字节，改完要跟着更新
    start = frames[0]["off"]
    if start + 190 <= len(out):
        crc = crc16_arc(bytes(out[start:start + 190]))
        out[pos + 34] = (crc >> 8) & 0xFF
        out[pos + 35] = crc & 0xFF
    return bytes(out)


def finalize(data, lead, expected, donor, plan):
    """编码收尾：CBR 换掉前导静音帧，再修正标签让解码长度分毫不差。"""
    replaced = 0
    if plan["mode"] == "cbr":
        data, replaced = make_spliced(data, lead, donor)
    return retime_tag(data, expected), replaced


def fit_end(pcm, end, cap, plan, donor, silence_rate, dst):
    """找结束于 end 的块：start 尽量小（块尽量长），但拼接后的体积不超过 cap。

    返回 (start, 体积, 替换帧数)。如果 [0, end] 整个放得下，返回 start=0，
    这一块就是第一块。试编码写到 dst，最后把最好的结果写回 dst。

    先按码率估一个起点，再用每次实测的体积校正「音乐每采样多少字节」——
    CBR 估得很准，ABR/VBR 的实际码率跟目标有出入，靠实测才收得住。
    """
    frame_bytes = 144000.0 * plan["est_kbps"] / SR  # 每帧音乐字节
    music_rate = plan["est_kbps"] * 1000.0 / 8 / SR  # 音乐每采样字节
    slope = music_rate - silence_rate                # 起点每后移 1 采样，体积减少这么多
    if slope <= 0:
        raise FitError("音乐码率（约 %d kbps）不比静音高，没法按大小切块"
                       % plan["est_kbps"])
    overhead = 3.0 * frame_bytes - (len(donor) if donor else 0)  # 标签帧 + 2 个保留静音帧
    if silence_rate * end >= cap - overhead:
        raise FitError("曲目太长：靠后的块光是前导静音就会超过 %.1f MiB"
                       % (cap / 1048576.0))

    t = int((music_rate * end - (cap - overhead)) / slope)
    t = max(0, min(t, end - 1))
    best = None       # 已知放得下的（最小的起点）结果
    too_big = -1      # 已知放不下的最大起点（再往小只会更大）
    fits = end        # 已知放得下的最小起点（再往大只会更小）
    for _ in range(12):
        if too_big + 1 >= fits:
            break
        t = min(max(t, too_big + 1), fits - 1)
        length = end - t
        if length < 1:
            break
        encode_pcm(padded_pcm(pcm, t, length), dst, plan)
        out, replaced = finalize(open(dst, "rb").read(), t, end, donor, plan)
        size = len(out)
        if end - t > 1:
            measured = (size - overhead - silence_rate * t) / (end - t)
            if measured > silence_rate:
                music_rate = measured
                slope = music_rate - silence_rate
        if size <= cap:
            best = (t, out, replaced, size)
            fits = t
            if t == 0:
                break
            t -= max(1, int((cap - size) / slope))      # 试着再往前长一点
        else:
            too_big = t
            t += max(2, int((size - cap) / slope) + 2)  # 缩短一点再试
    if best is None:
        raise FitError("放不下：结束于 %.3f 秒的块连静音都超过 %.1f MiB"
                       % (end / SR, cap / 1048576.0))
    with open(dst, "wb") as fh:
        fh.write(best[1])
    return best[0], best[3], best[2]


def check_chunk(path, lead, music_end, pcm):
    """回读校验：采样数、静音段全零、音乐段和源 PCM 的误差。

    第 k 块解码后应当等于「lead 个 0 + 源 PCM 的第 lead ~ music_end 个采样」，
    所以直接同下标比较即可（lead = 第 k 块前面垫的静音长度）。
    静音段最后两帧不检查：那里是音乐开头强瞬态的编码预回声（pre-echo），
    原方案（整块直接编码）同样存在，属于正常现象。
    """
    raw = decode_bytes(path)
    a = array.array("h")
    a.frombytes(raw)
    samples = len(a) // CH
    want = music_end

    zero_len = max(0, lead - 2 * MP3_SAMPLES) * FRAME_BYTES
    silent = raw[:zero_len].count(0) == zero_len

    err = sig = 0
    for i in range(lead, music_end, 8):
        j = i * CH
        e0 = a[j] - pcm[j]
        e1 = a[j + 1] - pcm[j + 1]
        err += e0 * e0 + e1 * e1
        sig += pcm[j] * pcm[j] + pcm[j + 1] * pcm[j + 1]
    snr = 10 * math.log10(sig / err) if err else float("inf")
    return samples, want, silent, snr


def remove_cache(path):
    """删掉一个不再需要的文件；删不掉只警告，不影响结果。返回是否删掉。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        print("   清理失败 %s: %s" % (path, e))
        return False
    return True


def is_ancestor(parent, child):
    """parent 是否就是 child，或者 child 的上级目录。"""
    parent = os.path.normcase(os.path.abspath(parent))
    child = os.path.normcase(os.path.abspath(child))
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:      # Windows 上不同盘符
        return False


def reset_output_dir(out_dir):
    """把输出目录整个删掉再重建，保证里面是空的。"""
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)


def parse_size(text):
    """解析 8M / 8MiB / 8388608 这类大小写法。"""
    t = text.strip().lower().replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([kmg]?)(?:i?b?)$", t)
    if not m:
        raise ValueError(text)
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2)])


def size_arg(text):
    try:
        size = parse_size(text)
    except ValueError:
        raise argparse.ArgumentTypeError("大小格式不对，可以写 8M / 8MiB / 8388608")
    if size <= 0:
        raise argparse.ArgumentTypeError("大小必须大于 0")
    return size


def bitrate_arg(text):
    """-b 320 / -b 320K / -b auto 都认，统一存成 "320k" / "auto"。"""
    v = text.strip().lower()
    if v == "auto":
        return "auto"
    if v.endswith("k"):
        v = v[:-1]
    try:
        kbps = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError("码率格式不对，可以写 auto / 320 / 320k")
    if kbps <= 0:
        raise argparse.ArgumentTypeError("码率必须大于 0")
    return "%gk" % kbps


def parse_kbps(text):
    """"320k" → 320；"auto" → None。"""
    if text is None or text == "auto":
        return None
    return float(text.rstrip("k"))


def build_parser():
    p = argparse.ArgumentParser(
        prog="split.py",
        description="大音频分块脚本：默认按大小把每块填到接近 8 MiB（不超过），"
                    "MP3 输入跟着它自己的 CBR/ABR/VBR 编码，其它输入用 VBR，"
                    "前导静音（只有 CBR 才替换）用 32 kbps，输出无缝播放用的 MP3。",
        epilog="命令行参数会覆盖脚本顶部「配置区」的默认值；相对路径按脚本所在目录解析。",
    )
    p.add_argument("-i", "--input", default=SRC, metavar="文件",
                   help="源音频文件（默认 %(default)s）")
    p.add_argument("-o", "--output-dir", default=OUT_DIR, metavar="目录",
                   help="输出目录（默认 %(default)s）")
    p.add_argument("-s", "--seconds-per-chunk", type=int, default=SECONDS_PER_CHUNK,
                   metavar="秒", help="按固定时长切块（优先于按大小切块）")
    p.add_argument("--max-size", type=size_arg, default=None, metavar="大小",
                   help="每块体积上限，如 8M / 8MiB / 8388608（默认 8MiB）；"
                        "不填 -s 时按它从后往前填满每块")
    p.add_argument("--name-fmt", default=NAME_FMT, metavar="格式",
                   help="输出文件名格式，{0} 是序号，从 1 开始（默认 %(default)s）")
    p.add_argument("--duration-file", default=DURATION_FILE, metavar="文件",
                   help="每块时长 JSON 的输出文件（默认 %(default)s）")
    p.add_argument("-m", "--music-mode", default=MUSIC_MODE, metavar="方式",
                   choices=["auto", "cbr", "abr", "vbr"],
                   help="音乐段的编码方式；auto = 跟着输入走：MP3 用它的 CBR/ABR/VBR，"
                        "其它格式用 VBR（默认 %(default)s）")
    p.add_argument("-b", "--music-bitrate", type=bitrate_arg, default=MUSIC_BITRATE,
                   metavar="码率",
                   help="音乐段目标码率；auto = 跟着输入（MP3 用它自己的码率，其它格式"
                        " 320k）。CBR/ABR 拿它当码率，VBR 拿它挑最接近的质量档"
                        "（手动 -m 换了方式又没给时按最高档：CBR/ABR 320k、VBR V0，"
                        "默认 %(default)s）")
    p.add_argument("--silence-bitrate", type=bitrate_arg, default=SILENCE_BITRATE,
                   metavar="码率", help="前导静音码率，只有 CBR 替换静音帧时用（默认 %(default)s）")
    return p


def encode_chunks(pcm, total, plan, cap, seconds, out_dir, args, donor, silence_rate):
    """按方案把整首编码成块，返回 (每块结束位置, 块信息列表)。

    块信息 = (start, length, 体积, 替换帧数, 目标路径)。
    """
    encoded = []
    if seconds is not None:
        step = seconds * SR
        cuts = list(range(0, total, step)) + [total]
        for k in range(len(cuts) - 1):
            start = cuts[k]
            length = cuts[k + 1] - start
            dst = os.path.join(out_dir, args.name_fmt.format(k + 1))
            try:
                encode_pcm(padded_pcm(pcm, start, length), dst, plan)
                out, replaced = finalize(open(dst, "rb").read(), start,
                                         cuts[k + 1], donor, plan)
                with open(dst, "wb") as fh:
                    fh.write(out)
            except Exception:
                remove_cache(dst)
                raise
            encoded.append((start, length, len(out), replaced, dst))
        return cuts, encoded

    found = []        # 从后往前：(start, 体积, 替换帧数, 工作文件)
    end = total
    j = 0
    while True:
        src = os.path.join(out_dir, str(j))
        start, size, replaced = fit_end(
            pcm, end, cap, plan, donor, silence_rate, src)
        found.append((start, size, replaced, src))
        if start == 0:
            break
        end = start
        j += 1
    found.reverse()
    cuts = [f[0] for f in found] + [total]
    for k, (start, size, replaced, src) in enumerate(found):
        dst = os.path.join(out_dir, args.name_fmt.format(k + 1))
        os.replace(src, dst)
        encoded.append((start, cuts[k + 1] - start, size, replaced, dst))
    print("   从后往前填满，共 %d 块" % len(found))
    return cuts, encoded


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.seconds_per_chunk is not None and args.seconds_per_chunk < 1:
        parser.error("--seconds-per-chunk 至少 1 秒")
    if args.max_size is not None and args.max_size < 64 * 1024:
        parser.error("--max-size 至少要 64K")
    if "{0}" not in args.name_fmt or os.path.dirname(args.name_fmt) or \
            not args.name_fmt.lower().endswith(".mp3"):
        parser.error("--name-fmt 必须是带 {0} 的 .mp3 文件名（不能带路径），例如 chunk-{0}.mp3")

    root = os.path.dirname(os.path.abspath(__file__))

    def resolve(path):
        return path if os.path.isabs(path) else os.path.join(root, path)

    src = resolve(args.input)
    out_dir = resolve(args.output_dir)
    duration_path = resolve(args.duration_file)

    if not os.path.isfile(src):
        sys.exit("找不到源文件: " + src)
    if is_ancestor(out_dir, root) or is_ancestor(out_dir, src):
        sys.exit("输出目录不能是项目目录、项目上级，或包含源文件（开始前会整个删掉）: " + out_dir)

    plan = choose_encoding(src, args.music_mode, args.music_bitrate)

    if args.seconds_per_chunk is not None:
        seconds = args.seconds_per_chunk
        limit = None
        cap = args.max_size if args.max_size is not None else MAX_CHUNK_BYTES
        mode_desc = "每块 %d 秒（体积上限 %.2f MiB）" % (seconds, cap / 1048576.0)
    else:
        seconds = None
        limit = args.max_size if args.max_size is not None else MAX_CHUNK_BYTES
        cap = limit
        mode_desc = "按大小，每块不超过 %.2f MiB（从后往前填满）" % (limit / 1048576.0)

    print("输入: %s" % src)
    print("输出: %s" % out_dir)
    print("音乐段: %s（%s）" % (plan_label(plan), plan["note"]))
    print("静音帧: %s" % (
        "%s，只替换 CBR 的前导静音" % args.silence_bitrate if plan["mode"] == "cbr"
        else "%s（VBR/ABR 不替换：编码器本来就把数字静音编成这个码率）"
             % args.silence_bitrate))
    print("切块: %s，文件名 %s\n" % (mode_desc, args.name_fmt))

    # 1) 解码源文件：PCM 直接走管道读进内存，不落 WAV
    print("1) 解码源文件（内存管道）")
    raw = decode_bytes(src)
    pcm = memoryview(raw).cast("h")
    total = len(pcm) // CH
    print("   %d 采样，%.3f 秒\n" % (total, total / SR))

    # 2) 编码 MP3：按大小切块时从后往前逐块填满，第一块是剩下的。
    #    CBR/ABR 放不下就整首改用 VBR 再来一遍。
    plans = [plan]
    if plan["mode"] != "vbr":
        plans.append(with_mode(plan, "vbr", plan["note"] + "；放不下，整首改用 VBR"))
    cuts = encoded = None
    for k, current in enumerate(plans):
        try:
            reset_output_dir(out_dir)      # 每次重来都先把上一次的块清掉
            donor, silence_rate = prepare_silence(current, args.silence_bitrate)
            print("2) 编码 MP3（音乐 %s，前导静音 %s）"
                  % (plan_label(current), args.silence_bitrate))
            cuts, encoded = encode_chunks(
                pcm, total, current, cap, seconds, out_dir, args, donor, silence_rate)
            over = [e for e in encoded if e[2] > cap]
            if over and current["mode"] != "vbr":
                raise FitError("%d 块超过 %.2f MiB 上限"
                               % (len(over), cap / 1048576.0))
            plan = current
            break
        except FitError as e:
            if k + 1 >= len(plans):
                sys.exit("%s 也放不下：%s" % (MODE_NAMES[current["mode"]], e))
            print("   %s 放不下（%s），整首改用 VBR 重来\n"
                  % (MODE_NAMES[current["mode"]], e))

    outs = []
    sizes = []
    for k, (start, length, size, replaced, dst) in enumerate(encoded):
        outs.append(dst)
        sizes.append(size)
        fill = "，%.1f%% 满" % (100.0 * size / limit) if limit else ""
        tail = "，替换 %d 帧静音" % replaced if plan["mode"] == "cbr" else ""
        print("   %s: 静音 %.3f s + 音乐 %.3f s = %.3f s，%.2f MiB%s%s"
              % (os.path.basename(dst), start / SR, length / SR, (start + length) / SR,
                 size / 1048576.0, fill, tail))
        if size > cap:
            print("   注意：这一块 %.2f MiB，超过了 %.2f MiB 的上限"
                  % (size / 1048576.0, cap / 1048576.0))

    # 每块的总时长（含前导静音，等于这一块在整曲时间线上的结束时间），
    # 输出成 JSON 浮点数组，方便导入 Scratch 列表。
    values = [round(end / SR, 3) for end in cuts[1:]]
    with open(duration_path, "wb") as fh:
        fh.write(json.dumps(values, separators=(",", ":")).encode("ascii"))
    print("   块时长列表: %s（%d 个浮点数，JSON）" % (duration_path, len(outs)))

    # 3) 校验：解码后的采样数、静音段是否全零、音乐和源 PCM 的误差、块体积
    print("\n3) 校验")
    ok = True
    for k, d in enumerate(outs):
        size = sizes[k]
        samples, want, silent, snr = check_chunk(d, cuts[k], cuts[k + 1], pcm)
        size_ok = size <= cap
        good = samples == want and silent and snr >= 6 and size_ok
        ok = ok and good
        extra = "" if size_ok else "，超过 %.2f MiB" % (cap / 1048576.0)
        print("   %s: 采样 %7d (期望 %7d)  静音段%s  SNR %5.1f dB  %s%s"
              % (os.path.basename(d), samples, want,
                 "全零" if silent else "有噪声", snr, "OK" if good else "BAD", extra))
    print("   共 %d 块，合计 %.2f MiB，最大一块 %.2f MiB（%s）" % (
        len(outs), sum(sizes) / 1048576.0, max(sizes) / 1048576.0, plan_label(plan)))

    print("\n总体:", "通过" if ok else "有问题")
    if not ok:
        return 1
    print("块数按大小自动决定为 %d 块，记得把 .sb3 里的声音数量也改成 %d"
          % (len(outs), len(outs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
