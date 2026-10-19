# -*- coding: utf-8 -*-
"""大音频分块脚本（WAV 精确切块 → 音乐 320 kbps + 前导静音 32 kbps）

流程：
  1. 原 MP3 → temp/source.wav（解码一次，得到完整 PCM）
  2. 从 WAV 按采样点精确切块：第 n 块 = n 分钟静音 + 第 n 分钟，写到 temp/wav/
  3. 每块单独编码成 MP3：音乐段和 input.mp3 一样用 320 kbps CBR，
     并加 -reservoir 0 关掉比特池（这样每一帧都能独立解码）
  4. 把前导静音里"无论怎样都肯定全在静音里"的帧，换成预生成的 32 kbps 静音帧
     （只是替换帧，帧数不变，所以解码后的采样数和音乐起点分毫不变）
  5. 回读校验：采样数、静音段是否全零、音乐段和源 PCM 的误差

为什么音质能和 input.mp3 一样：
      MP3 是有损格式，重新编码不可能逐字节无损；真无损只能用 WAV/FLAC，
      但 60 秒立体声 WAV 就超过 10 MB，会超过 8 MB 的上传限制。
      这里改用和原文件相同的 320 kbps CBR 重新编码，音乐段和源 PCM 的误差约
      38 dB SNR（原来的 V0 约 32 dB），远低于人耳能分辨的程度。

为什么静音可以几乎不占体积：
      LAME 默认会跨帧共用比特（比特池），一帧的数据可能引用前面几帧的字节，
      这种流没法从中间替换帧。用 -reservoir 0 编码后每帧自包含，
      就能把前面几分钟静音的帧整段换成 32 kbps 的静音帧：
      帧数不变 → 解码出来的采样数、音乐起点都不变，静音体积却只有 1/10
      （每分钟 0.24 MB）。

为什么这样能无缝：
      每块都是从 WAV 独立编码出的新文件，第一帧不依赖任何前文；编码器把延迟/
      填充写进了 LAME 标签，解码器会自动裁掉，所以每块解码出来就是精确的 PCM。
      替换静音帧只发生在音乐开始之前（后面还留了至少 2 帧原编码的静音），
      解码器在那里的状态本来就是全零，因此音乐部分逐采样不变。

用法：python split.py
"""

import array
import math
import os
import subprocess
import sys
import wave

# ============ 配置区 ============
SRC = "input.mp3"
OUT_DIR = "output-chunks"
TEMP_DIR = "temp"
SECONDS_PER_CHUNK = 60
NAME_FMT = "{0}.mp3"

# 音乐段码率：和 input.mp3 一致（320 kbps CBR）。
# 想更省体积可以改成 "256k" / "192k"，但离原文件会越来越远。
MUSIC_BITRATE = "320k"
# 前导静音码率：MPEG-1 立体声允许的最低码率，只影响体积，不影响音质。
SILENCE_BITRATE = "32k"
# ================================

SR = 44100
CH = 2
SAMPWIDTH = 2
FRAME_BYTES = CH * SAMPWIDTH

# MPEG-1 Layer III：每帧固定 1152 个采样
MP3_SAMPLES = 1152
BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
SAMPLE_RATES = [44100, 48000, 32000]


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


def mp3_frames(data):
    """拆出 MPEG-1 Layer III 的帧。

    返回 [{off, size, br, mdb, xing}, ...]：
      off/size  帧在文件里的位置和长度
      br        该帧码率（kbps）
      mdb       main_data_begin，0 表示这一帧不引用前面的字节（自包含）
      xing      Xing/Info 标签帧（元数据帧，不是音频）
    """
    frames = []
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
        frames.append(dict(off=off, size=size, br=BITRATES[br_i], mdb=mdb, xing=xing))
        off += size
    return frames


def make_silence_frame(temp):
    """生成一帧 32 kbps 的数字静音，用来替换前面几分钟的静音帧。"""
    path = os.path.join(temp, "silence.mp3")
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=%d:cl=stereo" % SR,
        "-t", "0.2", "-c:a", "libmp3lame", "-b:a", SILENCE_BITRATE,
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


def encode_chunk(wav, dst, lead, donor):
    """编码一块，并把前导静音里的帧换成低码率静音帧。返回替换的帧数。"""
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", wav,
        "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", MUSIC_BITRATE,
        "-reservoir", "0", "-ar", str(SR), "-ac", str(CH),
        "-map_metadata", "-1", "-id3v2_version", "0", "-write_id3v1", "0", dst])

    data = open(dst, "rb").read()
    frames = mp3_frames(data)
    if not frames or not frames[0]["xing"]:
        raise RuntimeError("编码结果里没有 Xing/Info 帧（无法可靠对齐）: " + dst)
    tag = data[frames[0]["off"]:frames[0]["off"] + frames[0]["size"]]
    audio = frames[1:]

    # 关掉比特池后，每一帧都必须是自包含的；否则替换静音帧会把音乐开头弄坏
    if any(f["mdb"] for f in audio):
        raise RuntimeError("比特池没关掉（-reservoir 0 没生效），不能替换静音帧: " + dst)

    # 只替换"就算编码器延迟到 1 帧，也肯定整帧都在静音里"的帧：
    # 末尾至少留 1 帧（实际会留 2 帧）原编码静音，保证音乐起点不受影响。
    replaced = min(max(0, lead // MP3_SAMPLES - 1), len(audio))
    if replaced:
        out = tag + donor * replaced + \
            b"".join(data[f["off"]:f["off"] + f["size"]] for f in audio[replaced:])
        with open(dst, "wb") as fh:
            fh.write(out)
    return replaced


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


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(root, SRC)
    out_dir = os.path.join(root, OUT_DIR)
    temp = os.path.join(root, TEMP_DIR)
    wav_dir = os.path.join(temp, "wav")

    if not os.path.isfile(src):
        sys.exit("找不到源文件: " + src)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(wav_dir, exist_ok=True)

    # 1) 解码成 WAV（缓存）
    src_wav = os.path.join(temp, "source.wav")
    print("1) 解码源文件 -> %s" % src_wav)
    sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
        "-map", "0:a:0", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", str(CH), src_wav])

    with wave.open(src_wav, "rb") as wf:
        total = wf.getnframes()
        pcm = array.array("h")
        pcm.frombytes(wf.readframes(total))
    pcm_bytes = pcm.tobytes()
    print("   %d 采样，%.3f 秒\n" % (total, total / SR))

    step = SECONDS_PER_CHUNK * SR
    cuts = list(range(0, total, step)) + [total]
    n = len(cuts) - 1

    zero_second = b"\x00" * (SR * FRAME_BYTES)

    # 2) 从 WAV 精确切块
    print("2) 切块（每块 %d 秒，采样点精确）" % SECONDS_PER_CHUNK)
    wavs = []
    for k in range(n):
        a, b = cuts[k], cuts[k + 1]
        path = os.path.join(wav_dir, "%d.wav" % k)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(CH)
            wf.setsampwidth(SAMPWIDTH)
            wf.setframerate(SR)
            left = a * FRAME_BYTES
            while left >= len(zero_second):
                wf.writeframes(zero_second)
                left -= len(zero_second)
            if left:
                wf.writeframes(zero_second[:left])
            wf.writeframes(pcm_bytes[a * FRAME_BYTES:b * FRAME_BYTES])
        wavs.append(path)
        print("   %d.wav: 静音 %.3f s + 音乐 %.3f s = %.3f s"
              % (k, a / SR, (b - a) / SR, b / SR))

    # 3) 每块单独编码 + 替换静音帧
    donor = make_silence_frame(temp)
    print("\n3) 编码 MP3（音乐 %s CBR，前导静音 %s）" % (MUSIC_BITRATE, SILENCE_BITRATE))
    outs = []
    for k, w in enumerate(wavs):
        dst = os.path.join(out_dir, NAME_FMT.format(k))
        replaced = encode_chunk(w, dst, cuts[k], donor)
        outs.append(dst)
        print("   %s  %.2f MB（替换 %d 帧静音）"
              % (os.path.basename(dst), os.path.getsize(dst) / 1048576, replaced))

    # 4) 校验：解码后的采样数、静音段是否全零、音乐和源 PCM 的误差
    print("\n4) 校验")
    ok = True
    total_size = 0
    for k, d in enumerate(outs):
        total_size += os.path.getsize(d)
        samples, want, silent, snr = check_chunk(d, cuts[k], cuts[k + 1], pcm)
        good = samples == want and silent and snr >= 20
        ok = ok and good
        print("   %d.mp3: 采样 %7d (期望 %7d)  静音段%s  SNR %5.1f dB  %s"
              % (k, samples, want, "全零" if silent else "有噪声", snr, "OK" if good else "BAD"))
    print("   合计 %.2f MB，最大一块 %.2f MB" % (
        total_size / 1048576,
        max(os.path.getsize(d) for d in outs) / 1048576))
    print("\n总体:", "通过" if ok else "有问题")


if __name__ == "__main__":
    main()
