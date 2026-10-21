# -*- coding: utf-8 -*-
"""大音频分块脚本（按大小填满每块 → 音乐按输入码率编码 + 32 kbps 前导静音）

流程：
  1. 原音频 → temp/source.wav（解码一次，得到完整 PCM）
  2. 默认按大小切块：一边试编码一边找边界，让每块「前导静音 + 音乐」编码后
     不超过 MAX_CHUNK_BYTES（默认 8 MiB），并且尽量把这一块填满。
     想按固定时长切就用 -s 60（旧行为）。
  3. 每块单独编码成 MP3：音乐段默认按输入文件的码率自动决定（MP3 输入用它的码率，
     其它格式用 320 kbps），并加 -reservoir 0 关掉比特池（这样每一帧都能独立解码）
  4. 把前导静音里"无论怎样都肯定全在静音里"的帧，换成预生成的 32 kbps 静音帧
     （只是替换帧，帧数不变，所以解码后的采样数和音乐起点分毫不变）
  5. 回读校验：采样数、静音段是否全零、音乐段和源 PCM 的误差

注意：按大小切块后块数会变（不再固定 8 块），.sb3 里的声音数量要一起改。

缓存：
      temp/ 里的中间文件在对应的步骤用完后立即删除；全部成功后如果 temp/ 已经空了，
      连 temp/ 文件夹一起删掉。中途出错时，还没用到的缓存会保留下来，方便排查/重跑。

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
import subprocess
import sys
import wave

# ============ 配置区（默认值，可被命令行参数覆盖） ============
SRC = "input.mp3"
OUT_DIR = "output-chunks"
TEMP_DIR = "temp"
NAME_FMT = "{0}.mp3"      # {0} 是块序号，从 1 开始（和 Scratch 里默认的声音编号一致）
# 每块时长的列表文件（给 Scratch 列表导入用），相对脚本目录。
DURATION_FILE = "output-chunks-duration.txt"

# 默认按大小切块：每块尽量填满，但不超过 MAX_CHUNK_BYTES。
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


def sh(args):
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s" % (" ".join(args), p.stderr))
    return p.stdout


def decode_bytes(path):
    """把音频解码成 s16le / 44.1 kHz / 立体声 的原始字节。"""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le",
                        "-ac", str(CH), "-ar", str(SR), "-"], capture_output=True)
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


def make_silence_frame(path, bitrate):
    """生成一帧低码率数字静音，用来替换前面几分钟的静音帧。"""
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=%d:cl=stereo" % SR,
        "-t", "0.2", "-c:a", "libmp3lame", "-b:a", bitrate,
        "-reservoir", "0", "-ar", str(SR), "-ac", str(CH),
        "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0", path])
    data = open(path, "rb").read()
    audio = [f for f in mp3_frames(data) if not f["xing"]]
    if not audio:
        raise RuntimeError("没能生成静音帧: " + path)
    if any(f["mdb"] != 0 for f in audio):
        raise RuntimeError("静音帧没有关掉比特池，不能独立解码: " + path)
    if any(decode_bytes(path)):
        raise RuntimeError("静音帧解码结果不是全零: " + path)
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


def fit_chunk(pcm, start, remaining, limit, bitrate, donor, tmp_mp3):
    """在不超过 limit 字节的前提下，找最长的音乐长度并返回 (length, 字节, 替换帧数)。

    做法：先按码率估一个长度，试编码，再根据实际大小加/减几帧，直到填得最满。
    """
    kbps = int(bitrate.rstrip("k"))
    bytes_per_sec = kbps * 1000.0 / 8
    frame_bytes = 144000.0 * kbps / SR       # 每帧平均字节数
    frame_max = int(frame_bytes) + 1         # 每帧最多字节数
    silence_bytes = max(0, start // MP3_SAMPLES - 1) * len(donor)

    # 初始估计：留 4 KB 给标签帧和音乐开头保留的静音帧
    length = int((limit - silence_bytes - 4096) / bytes_per_sec * SR)
    length = max(0, min(length, remaining))
    if length < MP3_SAMPLES:
        raise RuntimeError("第 %.1f 秒处放不下一块：光是前导静音就快占满 %.1f MiB 了，曲目太长"
                           % (start / SR, limit / 1048576.0))

    best = None
    for _ in range(6):
        encode_pcm(padded_pcm(pcm, start, length), tmp_mp3, bitrate)
        out, replaced = make_spliced(open(tmp_mp3, "rb").read(), start, donor)
        size = len(out)
        if size <= limit:
            if best is None or size > best[2]:
                best = (length, out, size, replaced)
            room = limit - size
            extra = int(room // frame_max)
            if extra <= 0 or length >= remaining:
                break
            length = min(length + extra * MP3_SAMPLES, remaining)
        else:
            if best is not None:
                break
            length -= (int((size - limit) // frame_bytes) + 2) * MP3_SAMPLES
            if length < MP3_SAMPLES:
                break
    if best is None:
        raise RuntimeError("第 %.1f 秒处放不下一块：%.1f MiB 的限额不够用，曲目太长或码率太高"
                           % (start / SR, limit / 1048576.0))
    return best


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
    """删掉一个不再需要的中间文件；删不掉只警告，不影响结果。返回是否删掉。"""
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as e:
        print("   清理失败 %s: %s" % (path, e))
        return False
    return True


def clean_stale_outputs(out_dir, name_fmt, count):
    """删掉输出目录里序号不在 1..count 里的旧块（比如上次更多块留下的 4..8、旧版留下的 0）。"""
    pattern = re.escape(name_fmt).replace(r"\{0\}", r"(\d+)")
    regex = re.compile("^" + pattern + "$")
    removed = 0
    for name in os.listdir(out_dir):
        m = regex.match(name)
        if not m:
            continue
        try:
            index = int(m.group(1))
        except ValueError:
            continue
        if (index < 1 or index > count) and remove_cache(os.path.join(out_dir, name)):
            removed += 1
    return removed


def prune_empty_dirs(*dirs):
    """删掉空目录（只删空的）。"""
    for d in dirs:
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass


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
    p.add_argument("-t", "--temp-dir", default=TEMP_DIR, metavar="目录",
                   help="中间文件目录（默认 %(default)s）")
    p.add_argument("-s", "--seconds-per-chunk", type=int, default=SECONDS_PER_CHUNK,
                   metavar="秒", help="按固定时长切块（不填就按 --max-size 的大小切块）")
    p.add_argument("--max-size", type=size_arg, default=None, metavar="大小",
                   help="按大小切块时每块的上限，如 8M / 8MiB / 8388608（默认 8MiB）")
    p.add_argument("--name-fmt", default=NAME_FMT, metavar="格式",
                   help="输出文件名格式，{0} 是序号，从 1 开始（默认 %(default)s）")
    p.add_argument("--duration-file", default=DURATION_FILE, metavar="文件",
                   help="每块时长列表的输出文件（默认 %(default)s）")
    p.add_argument("-b", "--music-bitrate", default=MUSIC_BITRATE, metavar="码率",
                   help="音乐段码率；auto = 按输入文件自动决定（默认 %(default)s）")
    p.add_argument("--silence-bitrate", default=SILENCE_BITRATE, metavar="码率",
                   help="前导静音码率（默认 %(default)s）")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.seconds_per_chunk is not None and args.max_size is not None:
        parser.error("-s 和 --max-size 只能选一个")
    if args.seconds_per_chunk is not None and args.seconds_per_chunk < 1:
        parser.error("--seconds-per-chunk 至少 1 秒")
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
    temp = resolve(args.temp_dir)
    duration_path = resolve(args.duration_file)
    src_wav = os.path.join(temp, "source.wav")
    donor_path = os.path.join(temp, "silence.mp3")
    cand_mp3 = os.path.join(temp, "chunk.mp3")

    if not os.path.isfile(src):
        sys.exit("找不到源文件: " + src)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(temp, exist_ok=True)

    if args.music_bitrate == "auto":
        music_bitrate, music_note = auto_music_bitrate(src)
    else:
        music_bitrate, music_note = args.music_bitrate, "手动指定"

    if args.seconds_per_chunk is not None:
        seconds = args.seconds_per_chunk
        limit = None
        mode_desc = "每块 %d 秒" % seconds
    else:
        seconds = None
        limit = args.max_size if args.max_size is not None else MAX_CHUNK_BYTES
        if limit < 64 * 1024:
            parser.error("--max-size 至少要 64K")
        mode_desc = "按大小，每块不超过 %.2f MiB（尽量填满）" % (limit / 1048576.0)

    print("输入: %s" % src)
    print("输出: %s" % out_dir)
    print("临时: %s" % temp)
    print("音乐段: %s（%s）" % (music_bitrate, music_note))
    print("切块: %s，静音 %s，文件名 %s\n"
          % (mode_desc, args.silence_bitrate, args.name_fmt))

    # 1) 解码成 WAV（缓存）
    print("1) 解码源文件 -> %s" % src_wav)
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
        "-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", str(CH), src_wav])

    with wave.open(src_wav, "rb") as wf:
        total = wf.getnframes()
        pcm = array.array("h")
        pcm.frombytes(wf.readframes(total))
    print("   %d 采样，%.3f 秒\n" % (total, total / SR))

    # 整曲 PCM 已经读进内存，source.wav 不再需要了
    cleaned = 1 if remove_cache(src_wav) else 0

    # 2) 逐块找边界 + 编码 + 替换静音帧
    donor = make_silence_frame(donor_path, args.silence_bitrate)
    print("2) 编码 MP3（音乐 %s CBR，前导静音 %s）" % (music_bitrate, args.silence_bitrate))
    outs = []
    cuts = [0]
    while cuts[-1] < total:
        start = cuts[-1]
        remaining = total - start
        if seconds is not None:
            length = min(seconds * SR, remaining)
            encode_pcm(padded_pcm(pcm, start, length), cand_mp3, music_bitrate)
            out, replaced = make_spliced(open(cand_mp3, "rb").read(), start, donor)
            size = len(out)
        else:
            length, out, size, replaced = fit_chunk(
                pcm, start, remaining, limit, music_bitrate, donor, cand_mp3)
        dst = os.path.join(out_dir, args.name_fmt.format(len(outs) + 1))
        with open(dst, "wb") as fh:
            fh.write(out)
        outs.append(dst)
        cuts.append(start + length)
        fill = "，%.1f%% 满" % (100.0 * size / limit) if limit else ""
        print("   %s: 静音 %.3f s + 音乐 %.3f s = %.3f s，%.2f MiB%s（替换 %d 帧静音）"
              % (os.path.basename(dst), start / SR, length / SR, (start + length) / SR,
                 size / 1048576.0, fill, replaced))
        if limit is None and size > MAX_CHUNK_BYTES:
            print("   注意：这一块超过了 %.0f MiB" % (MAX_CHUNK_BYTES / 1048576.0))
    cleaned += 1 if remove_cache(cand_mp3) else 0

    # 每块的总时长（含前导静音，等于这一块在整曲时间线上的结束时间），
    # 一行一个、LF 换行，方便直接导入 Scratch 列表。
    with open(duration_path, "wb") as fh:
        fh.write("\n".join("%.3f" % (end / SR) for end in cuts[1:]).encode("ascii"))
    print("   块时长列表: %s（%d 行，LF 换行）" % (duration_path, len(outs)))

    # 3) 校验：解码后的采样数、静音段是否全零、音乐和源 PCM 的误差
    print("\n3) 校验")
    ok = True
    total_size = 0
    for k, d in enumerate(outs):
        total_size += os.path.getsize(d)
        samples, want, silent, snr = check_chunk(d, cuts[k], cuts[k + 1], pcm)
        good = samples == want and silent and snr >= 6
        ok = ok and good
        print("   %s: 采样 %7d (期望 %7d)  静音段%s  SNR %5.1f dB  %s"
              % (os.path.basename(d), samples, want,
                 "全零" if silent else "有噪声", snr, "OK" if good else "BAD"))
    print("   共 %d 块，合计 %.2f MiB，最大一块 %.2f MiB" % (
        len(outs), total_size / 1048576.0,
        max(os.path.getsize(d) for d in outs) / 1048576.0))

    # 4) 清理缓存：静音帧用完就删，temp 空了连文件夹一起删
    print("\n4) 清理缓存")
    if ok:
        cleaned += 1 if remove_cache(donor_path) else 0
        stale = clean_stale_outputs(out_dir, args.name_fmt, len(outs))
        prune_empty_dirs(temp)
        print("   已删除 %d 个中间文件；%s"
              % (cleaned, "%s 已删除" % temp if not os.path.isdir(temp)
                 else "%s 里还有其它文件，保留" % temp))
        if stale:
            print("   已删除 %d 个序号不对的旧块（这次是 1–%d）" % (stale, len(outs)))
    else:
        print("   校验没通过，剩下的中间文件留在 %s，方便排查" % temp)

    print("\n总体:", "通过" if ok else "有问题")
    if ok:
        print("块数按大小自动决定为 %d 块，记得把 .sb3 里的声音数量也改成 %d"
              % (len(outs), len(outs)))


if __name__ == "__main__":
    main()
