# 大音频分块无缝播放

稽木世界（gitblock.cn）限制单个资源不能超过 8 MB，超过这个大小的音频不能整个上传。

以往多数音频分块播放的作品，在不同音频块衔接时会卡一下，也就是有一小段时间完全没有声音。

而这个作品做到了**无缝衔接**。

做法是用 FFmpeg 把音频从 0 秒起每满一分钟切一块，直到切完（当前这首切成 8 块，最大的约 3.3 MB，远低于限额），分块上传后在 Scratch 里同时播放。

## 目录结构

```
.
├─ input.mp3                  # 待处理的整曲（脚本只认这个文件名）
├─ split.py                   # 切块 + 编码
├─ duration.py                # 读音频时长（秒）
├─ output-chunks/             # 切块结果：0.mp3、1.mp3、…
│  └─ 0.mp3 … 7.mp3
├─ temp/                      # 中间文件（可随时删，会自动重建）
│  ├─ source.wav              # 整曲解码后的 PCM
│  └─ wav/                    # 每块的 WAV（带前导静音）
├─ 大音频分块无缝播放.sb3
└─ README.md
```

## 环境要求

- Python 3.8+，只用标准库，无需 `pip install`
- `ffmpeg` 和 `ffprobe` 在 `PATH` 里（需带 `libmp3lame` 编码器，gyan.dev 的 full build 即可）

验证：

```powershell
ffmpeg -version
ffprobe -version
python --version
```

## 快速开始

在项目目录下：

```powershell
# 1) 看整曲多长（秒），用来估算会切出几块
python duration.py

# 2) 切块并编码，输出到 output-chunks/
python split.py
```

`split.py` 跑完会打印每一块的时长、体积和校验结果，最后一行是 `总体: 通过` 或 `总体: 有问题`。

以当前这首 473.125 秒的曲子为例（`SECONDS_PER_CHUNK = 60`）：切出 8 块，前 7 块各 60 秒，第 8 块 53.125 秒。

## split.py

### 调用

```powershell
python split.py
```

无参数，所有输入输出都相对于脚本所在目录。文件名、切块长度等都在文件顶部的「配置区」改：

| 常量 | 默认值 | 说明 |
| --- | --- | --- |
| `SRC` | `"input.mp3"` | 源文件，相对脚本目录 |
| `OUT_DIR` | `"output-chunks"` | 输出目录，不存在会自动创建 |
| `TEMP_DIR` | `"temp"` | 中间文件目录 |
| `SECONDS_PER_CHUNK` | `60` | 每块秒数；改这个值要同步改 `.sb3`。60 秒在 VBR V0 下约 3 MB/块，给 8 MB 限额留足余量 |
| `NAME_FMT` | `"{0}.mp3"` | 输出文件名格式，`{0}` 是序号 |
| `LAME_QUALITY` | `"0"` | LAME VBR 质量，0 最好（约 245 kbps） |

采样率固定 44.1 kHz、双声道、16 bit。

### 它做了四件事

1. **解码一次**：`input.mp3` → `temp/source.wav`（PCM），后面全部基于这份 PCM，不再碰原 MP3。
2. **每分钟切一块**：从 0 秒起按 `SECONDS_PER_CHUNK`（默认 60 秒）切，直到切完；第 *n* 块 = 前 *n* 分钟静音 + 第 *n* 分钟的音乐，写到 `temp/wav/n.wav`。切点落在采样点上（60 × 44100），没有帧边界误差。
3. **逐块独立编码**：每块单独用 LAME VBR 编码成 `output-chunks/n.mp3`，去掉元数据和 ID3 标签。静音段会被自动压到最低码率，所以前几块体积也不大。
4. **回读校验**：把每块解码回 PCM，检查采样数是否等于期望值、第一个非零采样是否落在期望的起点（误差 < 30 ms），并打印每块体积和总体结论。

### 为什么这样能无缝

MP3 每帧的频谱数据依赖前面几帧（比特池）。直接按字节切原 MP3，每块开头都会缺前文，产生约 26 ms 的凹陷。这里的每块都是从 PCM 独立编码出的新文件，第一帧不依赖任何前文；编码器把延迟/填充写进了 LAME 标签，解码器会自动裁掉，所以每块解码出来就是精确的 PCM 长度。

代价是整曲被重新编码了一次（原来 320 kbps → VBR V0），不再逐字节无损。

### 在 Scratch 里怎么用

每块前面都垫了 `序号 × 60` 秒的静音，因此可以把 8 个声音**同时**开始播放：第 *n* 块要等 *n* 分钟才出声，任意时刻只有一块真正在响。这样就不依赖 Scratch 的定时器和「等待」精度，块与块之间也不会出现空隙。

换歌只需替换 `input.mp3` 后重跑 `split.py`；如果时长变了导致块数变化，记得在 `.sb3` 里同步增加/删除声音和对应逻辑。

## duration.py

### 调用

```powershell
python duration.py                        # 读同目录的 input.mp3
python duration.py output-chunks/3.mp3    # 读指定文件
```

输出秒数，保留三位小数，例如 `473.125`。

### 为什么要单独写一个

MP3 优先用 `ffprobe` 的时长；但切出来的小块往往没有时长索引（VBR 的 Xing/Info 头被去掉），`ffprobe` 会按首帧码率估算，结果偏差可能很大。这种情况下脚本改用「数 MPEG-1 Layer III 帧数 × 1152 / 采样率」算准确值（`frame_seconds`）。两者相差超过 0.5 秒时采用数帧的结果。

## 常见问题

- **`找不到源文件: …/input.mp3`**：确认源文件就叫 `input.mp3`，且和脚本在同一目录。
- **`命令失败: ffmpeg …`**：多半是 `ffmpeg` 不在 `PATH`，或输出文件被别的程序占用。
- **块数和 `.sb3` 对不上**：改 `SECONDS_PER_CHUNK` 或换了更长的歌之后，`sb3` 里的声音数量要一起改。
- **想省空间**：`temp/` 是纯中间产物，删掉下次运行会重新生成（但会重新解码一遍）。

## 关于

- `大音频分块无缝播放.sb3`：由作者 半岛的蒟蒻bddjr 手动编写。
- `split.py`、`duration.py`、`README.md`：由 DeepSeek-V4.1-Flash-1017 编写。
