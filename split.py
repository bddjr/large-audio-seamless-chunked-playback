# -*- coding: utf-8 -*-
"""大音频分块脚本（按大小从后往前填满每块 → 音乐按输入码率编码 + 32 kbps 前导静音）

流程：
  1. 原音频 → 内存里的 PCM（ffmpeg 管道解码一次，不落 WAV）
  2. 默认按大小切块：从最后一块往前逐块试编码，让每块「前导静音 + 音乐」编码后
     不超过 MAX_CHUNK_BYTES（默认 8 MiB）；最后几块填到尽量满，第一块是剩下的。
     想按固定时长切就用 -s 60（旧行为）。
  3. 每块单独编码成 MP3：音乐段默认按输入文件的码率自动决定（MP3 输入用它的码率，
     其它格式用 320 kbps），并加 -reservoir 0 关掉比特池（这样每一帧都能独立解码）
  4. 把前导静音里"无论怎样都肯定全在静音里"的帧，换成预生成的 32 kbps 静音帧
     （只是替换帧，帧数不变，所以解码后的采样数和音乐起点分毫不变）
  5. 回读校验：采样数、静音段是否全零、音乐段和源 PCM 的误差

注意：按大小切块后块数会变（不再固定 8 块），.sb3 里的声音数量要一起改。

过程不生成 temp 目录：PCM 和静音帧都在内存里；反着填时先写工作文件，
算完块数再改名成正式块。

为什么音质能和 input.mp3 一样：
      MP3 是有损格式，重新编码不可能逐字节无损；真无损只能用 WAV/FLAC，
      但 60 秒立体声 WAV 就超过 10 MiB，会超过 8 MiB 的上传限制。
      音乐段默认按输入的码率编码：320k 输入 → 320k 输出（约 38 dB SNR），
      128k 输入 → 128k 输出（约 22 dB，会再多一点损失）。想尽量保音质、不在乎体积时，
      可以用 -b 320k 强制按 320k 编码。

为什么静音可以几乎不占体积：
      LAME 默认会跨帧共用比特（比特池），一帧的数据可能引用前面几帧的字节，
      这种流没法从中间替换帧。用 -reservoir 0 编码后每帧自包含，
      就能把前面几分钟静音的帧整段换成 32 kbps 的静音帧：
      帧数不变 → 解码出来的采样数、音乐起点都不变，静音部分固定只按 32 kbps 算
      （每分钟约 0.23 MiB）。

为什么这样能无缝：
      每块都是从 PCM 独立编码出的新文件，第一帧不依赖任何前文；编码器把延迟/
      填充写进了 LAME 标签，解码器会自动裁掉，所以每块解码出来就是精确的 PCM。
      替换静音帧只发生在音乐开始之前（后面还留了至少 2 帧原编码的静音），
      解码器在那里的状态本来就是全零，因此音乐部分逐采样不变。

用法：
      python split.py
      python split.py --help
      python split.py -s 60 -i 另一首.mp3 -o out
      python split.py --max-size 4M
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

# 音乐段码率："auto" = 按输入文件自动决定（MP3 用它自己的码率，其它格式用 320k）。
# 也可以直接写 "320k" / "256k" / "192k" 强制指定。
MUSIC_BITRATE = "auto"
# 前导静音码率：MPEG-1 立体声允许的最低码率，只影响体积，不影响音质。
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
        yield dict(off=off, size=size, br=BITRATES[br_i], sr=sr, mdb=mdb, xing=xing)
        off += size


def mp3_frames(data):
    """拆出 MPEG-1 Layer III 的帧，返回列表。"""
    return list(iter_mp3_frames(data))


def input_mp3_stats(data):
    """统计输入 MP3 的码率：音频帧数、平均码率（kbps）、最低/最高帧码率。"""
    frames = 0
    total = 0
    low = high = None
    sr = SR
    for f in iter_mp3_frames(data):
        if f["xing"]:
            continue
        frames += 1
        total += f["size"]
        low = f["br"] if low is None else min(low, f["br"])
        high = f["br"] if high is None else max(high, f["br"])
        sr = f["sr"]
    if not frames:
        return 0, 0.0, None, None
    avg = total * 8 * sr / (frames * MP3_SAMPLES) / 1000.0
    return frames, avg, low, high


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
    best = 32
    for b in (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320):
        if abs(b - kbps) < abs(best - kbps):
            best = b
    return best


def auto_music_bitrate(src):
    """按输入文件决定音乐段码率，返回 (码率字符串, 说明)。

    MP3 输入：CBR 用它的码率，VBR 用平均码率，取最接近的标准档。
    其它格式（WAV/FLAC 等）：用 320k。
    """
    codec, br = probe_codec(src)
    if codec is None:
        return "320k", "读不出输入格式（ffprobe 不可用？）"
    if codec != "mp3":
        return "320k", "输入不是 MP3（%s）" % codec
    with open(src, "rb") as fh:
        data = fh.read()
    frames, avg, low, high = input_mp3_stats(data)
    if frames >= 10:
        if low == high:
            target, desc = low, "输入 mp3 CBR %d kbps" % low
        else:
            target, desc = avg, "输入 mp3 VBR 平均 %.0f kbps，最高 %d" % (avg, high)
    elif br:
        target, desc = br / 1000.0, "输入 mp3 %.0f kbps" % (br / 1000.0)
    else:
        return "320k", "输入 mp3 的码率读不出来"
    return "%dk" % nearest_bitrate(target), desc


def decode_mp3_data(data):
    """把 MP3 字节从管道解码成 s16le / 44.1 kHz / 立体声 的原始字节。"""
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "mp3", "-i", "pipe:0",
                        "-f", "s16le", "-ac", str(CH), "-ar", str(SR), "-"],
                       input=data, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("解码失败: " + p.stderr.decode("utf-8", "replace"))
    return p.stdout[: len(p.stdout) // 2 * 2]


def make_silence_frame(bitrate):
    """在内存里生成一帧低码率数字静音，用来替换前面几分钟的静音帧。"""
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


def encode_pcm(data, dst, bitrate):
    """把 s16le PCM 从管道喂给 ffmpeg，编码成 CBR MP3（关掉比特池）。"""
    p = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "s16le", "-ar", str(SR), "-ac", str(CH), "-i", "pipe:0",
         "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", bitrate,
         "-reservoir", "0", "-ar", str(SR), "-ac", str(CH),
         "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0", dst],
        input=data, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("编码失败: %s\n%s" % (dst, p.stderr.decode("utf-8", "replace")))


def make_spliced(data, lead, donor):
    """把前导静音里整帧都在静音区的帧换成低码率静音帧，返回 (字节, 替换帧数)。"""
    frames = mp3_frames(data)
    if not frames or not frames[0]["xing"]:
        raise RuntimeError("编码结果里没有 Xing/Info 帧（无法可靠对齐）")
    audio = frames[1:]

    # 关掉比特池后，每一帧都必须是自包含的；否则替换静音帧会把音乐开头弄坏
    if any(f["mdb"] for f in audio):
        raise RuntimeError("比特池没关掉（-reservoir 0 没生效），不能替换静音帧")

    # 只替换"就算编码器延迟到 1 帧，也肯定整帧都在静音里"的帧：
    # 末尾至少留 1 帧（实际会留 2 帧）原编码静音，保证音乐起点不受影响。
    replaced = min(max(0, lead // MP3_SAMPLES - 1), len(audio))
    tag = data[frames[0]["off"]:frames[0]["off"] + frames[0]["size"]]
    body = b"".join(data[f["off"]:f["off"] + f["size"]] for f in audio[replaced:])
    return tag + donor * replaced + body, replaced


def fit_end(pcm, end, cap, bitrate, donor, dst):
    """找结束于 end 的块：start 尽量小（块尽量长），但拼接后的体积不超过 cap。

    返回 (start, 体积, 替换帧数)。如果 [0, end] 整个放得下，返回 start=0，
    这一块就是第一块。试编码写到 dst，最后把最好的结果写回 dst。
    """
    kbps = int(bitrate.rstrip("k"))
    frame_bytes = 144000.0 * kbps / SR           # 每帧音乐字节
    music_rate = kbps * 1000.0 / 8 / SR          # 音乐每采样字节
    silence_rate = len(donor) / float(MP3_SAMPLES)  # 静音每采样字节
    slope = music_rate - silence_rate            # 起点每后移 1 采样，体积减少这么多
    if slope <= 0:
        raise RuntimeError("码率太低，没法按大小切块")
    overhead = 3.0 * frame_bytes - len(donor)     # 标签帧 + 2 个保留静音帧
    if silence_rate * end >= cap - overhead:
        raise RuntimeError("曲目太长：靠后的块光是前导静音就会超过 %.1f MiB"
                           % (cap / 1048576.0))

    # 先按公式估一个起点，再按实测体积微调
    t = int((music_rate * end - (cap - overhead)) / slope)
    t = max(0, min(t, end - 1))
    best = None
    for _ in range(8):
        length = end - t
        if length < 1:
            break
        encode_pcm(padded_pcm(pcm, t, length), dst, bitrate)
        out, replaced = make_spliced(open(dst, "rb").read(), t, donor)
        size = len(out)
        if size <= cap:
            if best is None or size > best[2]:
                best = (t, out, replaced, size)
            if t == 0:
                break
            step = int((cap - size) / slope)
            if step <= 0:
                break
            t = max(0, t - step)
        else:
            if best is not None:
                break
            step = int((size - cap) / slope) + 2
            t = min(end - 1, t + step)
    if best is None:
        raise RuntimeError("放不下：结束于 %.3f 秒的块连静音都超过 %.1f MiB"
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


def normalize_bitrate(value):
    """-b 320 / -b 320K 都当成 320k，"AUTO" 当成 auto。"""
    v = value.strip().lower()
    if v.isdigit():
        v += "k"
    return v


def build_parser():
    p = argparse.ArgumentParser(
        prog="split.py",
        description="大音频分块脚本：默认按大小把每块填到接近 8 MiB（不超过），"
                    "音乐段按输入码率编码、前导静音 32 kbps，输出无缝播放用的 MP3。",
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
    p.add_argument("-b", "--music-bitrate", default=MUSIC_BITRATE, metavar="码率",
                   help="音乐段码率；auto = 按输入文件自动决定（默认 %(default)s）")
    p.add_argument("--silence-bitrate", default=SILENCE_BITRATE, metavar="码率",
                   help="前导静音码率（默认 %(default)s）")
    return p


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
    args.music_bitrate = normalize_bitrate(args.music_bitrate)
    args.silence_bitrate = normalize_bitrate(args.silence_bitrate)

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
    reset_output_dir(out_dir)

    if args.music_bitrate == "auto":
        music_bitrate, music_note = auto_music_bitrate(src)
    else:
        music_bitrate, music_note = args.music_bitrate, "手动指定"

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
    print("输出: %s（已清空重建）" % out_dir)
    print("音乐段: %s（%s）" % (music_bitrate, music_note))
    print("切块: %s，静音 %s，文件名 %s\n"
          % (mode_desc, args.silence_bitrate, args.name_fmt))

    # 1) 解码源文件：PCM 直接走管道读进内存，不落 WAV
    print("1) 解码源文件（内存管道）")
    raw = decode_bytes(src)
    pcm = memoryview(raw).cast("h")
    total = len(pcm) // CH
    print("   %d 采样，%.3f 秒\n" % (total, total / SR))

    # 2) 编码 MP3：按大小切块时从后往前逐块填满，第一块是剩下的
    donor = make_silence_frame(args.silence_bitrate)
    print("2) 编码 MP3（音乐 %s CBR，前导静音 %s）" % (music_bitrate, args.silence_bitrate))
    encoded = []          # (start, length, 体积, 替换帧数, 目标路径)
    if seconds is not None:
        step = seconds * SR
        cuts = list(range(0, total, step)) + [total]
        for k in range(len(cuts) - 1):
            start = cuts[k]
            length = cuts[k + 1] - start
            dst = os.path.join(out_dir, args.name_fmt.format(k + 1))
            try:
                encode_pcm(padded_pcm(pcm, start, length), dst, music_bitrate)
                out, replaced = make_spliced(open(dst, "rb").read(), start, donor)
                with open(dst, "wb") as fh:
                    fh.write(out)
            except Exception:
                remove_cache(dst)
                raise
            encoded.append((start, length, len(out), replaced, dst))
    else:
        found = []        # 从后往前：(start, 体积, 替换帧数, 工作文件)
        end = total
        j = 1
        temp_mp3_name_prefix = "-" if "-{0}.mp3" != args.name_fmt.strip().lower() else "_"
        while True:
            src = os.path.join(out_dir, temp_mp3_name_prefix + str(j) + ".mp3")
            start, size, replaced = fit_end(
                pcm, end, cap, music_bitrate, donor, src)
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

    outs = []
    sizes = []
    for k, (start, length, size, replaced, dst) in enumerate(encoded):
        outs.append(dst)
        sizes.append(size)
        fill = "，%.1f%% 满" % (100.0 * size / limit) if limit else ""
        print("   %s: 静音 %.3f s + 音乐 %.3f s = %.3f s，%.2f MiB%s（替换 %d 帧静音）"
              % (os.path.basename(dst), start / SR, length / SR, (start + length) / SR,
                 size / 1048576.0, fill, replaced))
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
    print("   共 %d 块，合计 %.2f MiB，最大一块 %.2f MiB" % (
        len(outs), sum(sizes) / 1048576.0, max(sizes) / 1048576.0))

    print("\n总体:", "通过" if ok else "有问题")
    if not ok:
        return 1
    print("块数按大小自动决定为 %d 块，记得把 .sb3 里的声音数量也改成 %d"
          % (len(outs), len(outs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
