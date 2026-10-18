# -*- coding: utf-8 -*-
"""大音频分块脚本（WAV 精确切块 → 每块单独编码 MP3）

流程：
  1. 原 MP3 → temp/source.wav（解码一次，得到完整 PCM）
  2. 从 WAV 按采样点精确切块：第 n 块 = n 分钟静音 + 第 n 分钟，写到 temp/wav/
  3. 每块单独编码成 MP3（VBR，静音段自动压到最低码率），输出到 output-chunks/

为什么这样能无缝：
      MP3 每帧的频谱数据依赖前面几帧（比特池）。直接切原 MP3 会让每块第一帧
      缺前文，产生约 26 ms 的凹陷。这里每块都是从 WAV 独立编码出来的新文件，
      第一帧不依赖任何前文，且编码器把延迟/填充写进了 LAME 标签，解码器会
      自动裁掉，所以每块解码出来就是精确的 PCM 长度，8 块叠加严丝合缝。

代价：音频被重新编码了一次（原 320kbps → VBR V0），不再逐字节无损。

用法：python split.py
"""

import array
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
LAME_QUALITY = "0"      # VBR 质量（0 最好，约 245kbps；静音会自动降到 32kbps）
# ================================

SR = 44100
CH = 2
SAMPWIDTH = 2
FRAME_BYTES = CH * SAMPWIDTH


def sh(args):
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s" % (" ".join(args), p.stderr))
    return p.stdout


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
        pcm = wf.readframes(total)
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
            wf.writeframes(pcm[a * FRAME_BYTES:b * FRAME_BYTES])
        wavs.append(path)
        print("   %d.wav: 静音 %.3f s + 音乐 %.3f s = %.3f s"
              % (k, a / SR, (b - a) / SR, b / SR))

    # 3) 每块单独编码 MP3
    print("\n3) 编码 MP3（VBR q=%s，静音自动最低码率）" % LAME_QUALITY)
    outs = []
    for k, w in enumerate(wavs):
        dst = os.path.join(out_dir, NAME_FMT.format(k))
        sh(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", w,
            "-map", "0:a:0", "-c:a", "libmp3lame", "-q:a", LAME_QUALITY,
            "-ar", str(SR), "-ac", str(CH), "-map_metadata", "-1",
            "-id3v2_version", "0", "-write_id3v1", "0", dst])
        outs.append(dst)
        print("   %s  %.2f MB" % (os.path.basename(dst), os.path.getsize(dst) / 1048576))

    # 4) 校验：解码后的采样数、有声起点
    print("\n4) 校验")
    ok = True
    total_size = 0
    for k, d in enumerate(outs):
        total_size += os.path.getsize(d)
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", d, "-f", "s16le",
                            "-ac", str(CH), "-ar", str(SR), "-"], capture_output=True)
        a = array.array("h")
        a.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
        samples = len(a) // CH
        first = None
        for i in range(0, len(a), CH):
            if a[i] != 0 or a[i + 1] != 0:
                first = i // CH
                break
        start = first / SR if first is not None else -1
        want_samples = cuts[k + 1]
        want_start = cuts[k] / SR
        good = abs(samples - want_samples) < 200 and abs(start - want_start) < 0.03
        ok = ok and good
        print("   %d.mp3: 采样 %7d (期望 %7d)  有声起点 %.4f s (期望 %.4f)  %s"
              % (k, samples, want_samples, start, want_start, "OK" if good else "BAD"))
    print("   合计 %.2f MB" % (total_size / 1048576))
    print("\n总体:", "通过" if ok else "有问题")


if __name__ == "__main__":
    main()
