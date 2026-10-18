# -*- coding: utf-8 -*-
"""读取音频时长（秒，保留三位小数）。

用法：
    python duration.py                     # 默认读同目录的 input.mp3
    python duration.py 某个文件.mp3        # 也可以指定文件

说明：MP3 优先用 ffprobe 的时长；如果文件没有时长索引（比如分块后的小文件），
      ffprobe 会按首帧码率估算而算错，此时改用“数帧数 × 1152 / 44100”的准确值。
"""

import os
import subprocess
import sys

DEFAULT = "input.mp3"

BITRATES = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
SAMPLE_RATES = [44100, 48000, 32000]


def ffprobe_seconds(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True,
        ).stdout.strip()
        return float(out) if out else None
    except (OSError, ValueError):
        return None


def frame_seconds(path):
    """按 MPEG-1 Layer III 帧数计算时长；不是 MP3 就返回 None。"""
    with open(path, "rb") as fh:
        data = fh.read()
    if len(data) < 4 or not path.lower().endswith(".mp3"):
        return None
    off = 0
    frames = 0
    n = len(data)
    first_rate = None
    while off + 6 <= n:
        if data[off] != 0xFF or (data[off + 1] & 0xE0) != 0xE0:
            off += 1
            continue
        ver = (data[off + 1] >> 3) & 3
        layer = (data[off + 1] >> 1) & 3
        br_i = (data[off + 2] >> 4) & 0xF
        sr_i = (data[off + 2] >> 2) & 3
        if ver != 3 or layer != 1 or br_i == 0 or br_i == 15 or sr_i == 3:
            off += 1
            continue
        sr = SAMPLE_RATES[sr_i]
        size = 144000 * BITRATES[br_i] // sr + ((data[off + 2] >> 1) & 1)
        if first_rate is None:
            first_rate = sr
        frames += 1
        off += size
    if not frames:
        return None
    return frames * 1152 / first_rate


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if not os.path.isabs(path):
        path = os.path.join(here, path)
    if not os.path.isfile(path):
        sys.exit("找不到文件: " + path)

    seconds = ffprobe_seconds(path)
    exact = frame_seconds(path)
    # ffprobe 对没有索引的文件会估错，偏差大就用数帧的准确值
    if exact is not None and (seconds is None or abs(seconds - exact) > 0.5):
        seconds = exact

    if seconds is None:
        sys.exit("读取失败: " + path)
    print("%.3f" % seconds)


if __name__ == "__main__":
    main()
