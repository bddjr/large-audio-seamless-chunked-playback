# -*- coding: utf-8 -*-
"""把 output-chunks/ 里的分块和时长灌进 .sb3。

只改数据，不动积木：
  - 声音 1..N：替换成 output-chunks/{n}.mp3，更新 assetId / md5ext / sampleCount
  - 列表 output-chunks-duration：替换成每块的结束时间（含前导静音），存浮点数
  - 变量 音频时长：替换成总时长
  - 舞台变量 播放进度 = 00:00.000，播放第几块音频 = 1，
    距离播完这块音频还剩 = 第 1 块时长
  - 角色临时变量 tmp / 计时器转显示格式输出 / 计算距离… / 计时器 清空

声音数量比 N 多就删掉多余的，少就补上；其它声音、造型和积木不动。
默认覆盖原 .sb3，用 -o 可以写到新文件。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
SB3 = "大音频分块无缝播放.sb3"
CHUNKS = "output-chunks"
DURATION_FILE = "output-chunks-duration.json"
NAME_FMT = "{0}.mp3"
LIST_NAME = "output-chunks-duration"
TOTAL_VAR = "音频时长"
TEMP_VARS = ("tmp", "计时器转显示格式输出", "计算距离播完这块音频还剩", "计时器")
DIGIT = re.compile(r"^\d+$")


def resolve(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_durations(path):
    with open(path, "rb") as fh:
        try:
            data = json.loads(fh.read().decode("utf-8"))
        except ValueError:
            raise RuntimeError("时长文件不是合法 JSON: " + path)
    if not isinstance(data, list) or not data:
        raise RuntimeError("时长 JSON 必须是非空数组: " + path)
    for value in data:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError("时长 JSON 里有非数字: %r" % (value,))
    return data


def chunk_paths(chunks_dir, name_fmt, count):
    paths = []
    for k in range(1, count + 1):
        path = os.path.join(chunks_dir, name_fmt.format(k))
        if not os.path.isfile(path):
            raise RuntimeError("找不到分块: " + path)
        paths.append(path)
    return paths


def find_target(targets, test):
    for target in targets:
        if test(target):
            return target
    return None


def fmt_clock(seconds):
    """秒数 → 00:00.000 格式（和舞台上的显示一致）。"""
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return "%02d:%06.3f" % (minutes, rest)


def set_stage_values(project, durations):
    """改舞台全局变量的值（不动积木）。"""
    stage = next((t for t in project.get("targets", []) if t.get("isStage")), None)
    if stage is None:
        return
    values = {
        "播放进度": "00:00.000",
        "播放第几块音频": 1,
        "距离播完这块音频还剩": fmt_clock(durations[0]),
    }
    for vid, var in stage.get("variables", {}).items():
        if var[0] not in values:
            continue
        var[1] = values[var[0]]
        for monitor in project.get("monitors", []):
            if monitor.get("id") == vid:
                monitor["value"] = values[var[0]]


def clear_temp_vars(project):
    """把角色里的临时变量清空（音频时长保留）。"""
    for target in project.get("targets", []):
        if target.get("isStage"):
            continue
        for var in target.get("variables", {}).values():
            if var[0] in TEMP_VARS:
                var[1] = ""


def inject(sb3_path, chunks, durations, name_fmt):
    with zipfile.ZipFile(sb3_path) as z:
        files = {name: z.read(name) for name in z.namelist()}
    if "project.json" not in files:
        raise RuntimeError("sb3 里没有 project.json")
    project = json.loads(files["project.json"].decode("utf-8"))
    set_stage_values(project, durations)
    clear_temp_vars(project)
    targets = project.get("targets", [])

    sound_target = find_target(
        targets, lambda t: any(DIGIT.match(s.get("name", "")) for s in t.get("sounds", [])))
    if sound_target is None:
        raise RuntimeError("sb3 里找不到名字是数字的声音（1、2、…）")

    old_sounds = sound_target.get("sounds", [])
    old_digit = {s.get("name"): s for s in old_sounds if DIGIT.match(s.get("name", ""))}
    old_assets = {s.get("md5ext") for s in old_digit.values() if s.get("md5ext")}
    default_rate = 192000
    for sound in old_digit.values():
        default_rate = sound.get("rate", default_rate)
        break

    new_sounds = []
    new_assets = {}
    for k, (path, duration) in enumerate(zip(chunks, durations), 1):
        with open(path, "rb") as fh:
            data = fh.read()
        md5 = hashlib.md5(data).hexdigest()
        md5ext = md5 + ".mp3"
        sound = old_digit.get(str(k)) or {}
        rate = sound.get("rate", default_rate) or default_rate
        sound.update({
            "name": str(k),
            "assetId": md5,
            "dataFormat": "mp3",
            "rate": rate,
            "sampleCount": int(round(duration * rate)),
            "md5ext": md5ext,
        })
        new_sounds.append(sound)
        new_assets[md5ext] = data

    others = [s for s in old_sounds if not DIGIT.match(s.get("name", ""))]
    sound_target["sounds"] = others + new_sounds

    list_holder = None
    for target in targets:
        for key, value in target.get("lists", {}).items():
            if value[0] == LIST_NAME:
                value[1] = list(durations)
                list_holder = target
                break
    if list_holder is None:
        raise RuntimeError("sb3 里找不到列表 " + LIST_NAME)

    total = durations[-1]
    total_found = False
    for target in targets:
        for value in target.get("variables", {}).values():
            if value[0] == TOTAL_VAR:
                value[1] = total
                total_found = True

    referenced = set()
    for target in targets:
        for sound in target.get("sounds", []):
            if sound.get("md5ext"):
                referenced.add(sound["md5ext"])
        for costume in target.get("costumes", []):
            if costume.get("md5ext"):
                referenced.add(costume["md5ext"])
    drop = old_assets - referenced - set(new_assets)

    project_bytes = json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return files, project_bytes, new_assets, drop, len(new_sounds), total_found


def write_sb3(out_path, files, project_bytes, new_assets, drop):
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr("project.json", project_bytes, zipfile.ZIP_DEFLATED)
        for name, data in files.items():
            if name == "project.json" or name in new_assets or name in drop:
                continue
            kind = zipfile.ZIP_STORED if name.endswith(".mp3") else zipfile.ZIP_DEFLATED
            z.writestr(name, data, kind)
        for name, data in new_assets.items():
            z.writestr(name, data, zipfile.ZIP_STORED)


def verify(out_path, count, durations):
    expected = list(durations)
    with zipfile.ZipFile(out_path) as z:
        project = json.loads(z.read("project.json").decode("utf-8"))
        names = set(z.namelist())
        target = find_target(
            project["targets"],
            lambda t: any(DIGIT.match(s.get("name", "")) for s in t.get("sounds", [])))
        sounds = {s["name"]: s for s in target.get("sounds", []) if DIGIT.match(s["name"])}
        for k in range(1, count + 1):
            sound = sounds.get(str(k))
            if sound is None:
                raise RuntimeError("校验失败：没有声音 %d" % k)
            if sound["md5ext"] not in names:
                raise RuntimeError("校验失败：声音 %d 的资源不在 sb3 里" % k)
            data = z.read(sound["md5ext"])
            if hashlib.md5(data).hexdigest() != sound["assetId"]:
                raise RuntimeError("校验失败：声音 %d 的 md5 对不上" % k)
        if len(sounds) != count:
            raise RuntimeError("校验失败：声音数量是 %d，不是 %d" % (len(sounds), count))
        for target in project["targets"]:
            for value in target.get("lists", {}).values():
                if value[0] == LIST_NAME and value[1] != expected:
                    raise RuntimeError("校验失败：列表内容和时长文件不一致")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把 output-chunks/ 的分块和时长灌进 .sb3（声音、列表、总时长变量）")
    parser.add_argument("-s", "--sb3", default=SB3, metavar="文件",
                        help="目标 sb3（默认 %(default)s）")
    parser.add_argument("-c", "--chunks", default=CHUNKS, metavar="目录",
                        help="分块目录（默认 %(default)s）")
    parser.add_argument("--duration-file", default=DURATION_FILE, metavar="文件",
                        help="每块时长 JSON（默认 %(default)s）")
    parser.add_argument("--name-fmt", default=NAME_FMT, metavar="格式",
                        help="分块文件名格式（默认 %(default)s）")
    parser.add_argument("-o", "--output", default=None, metavar="文件",
                        help="写到这里（默认覆盖 --sb3）")
    args = parser.parse_args(argv)

    sb3_path = resolve(args.sb3)
    chunks_dir = resolve(args.chunks)
    duration_path = resolve(args.duration_file)
    if not os.path.isfile(sb3_path):
        sys.exit("找不到 sb3: " + sb3_path)

    durations = load_durations(duration_path)
    chunks = chunk_paths(chunks_dir, args.name_fmt, len(durations))
    out_path = resolve(args.output) if args.output else sb3_path

    files, project_bytes, new_assets, drop, count, total_found = inject(
        sb3_path, chunks, durations, args.name_fmt)
    write_sb3(out_path, files, project_bytes, new_assets, drop)
    verify(out_path, count, durations)

    print("已写入: %s" % out_path)
    print("  声音 1..%d，共 %.2f MiB" % (
        count, sum(len(d) for d in new_assets.values()) / 1048576.0))
    print("  列表 %s = %s" % (LIST_NAME, ", ".join("%.3f" % d for d in durations)))
    if total_found:
        print("  变量 %s = %s" % (TOTAL_VAR, durations[-1]))
    else:
        print("  警告: 没找到变量 %s，总时长没有更新" % TOTAL_VAR)
    print("  删掉旧声音资源: %d 个" % len(drop))


if __name__ == "__main__":
    sys.exit(main())
