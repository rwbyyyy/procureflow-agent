"""免费 Edge 普通话配音，按既有六段画面时间对齐并合成新视频。

运行：uv run python add_voiceover.py。语音工具由 uv 独立缓存，
不修改 Agent 的运行依赖；不使用任何 API Key。需要联网和 FFmpeg。
"""

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARTIFACTS = ROOT / "artifacts"
VOICE = "zh-CN-YunxiNeural"
SEGMENTS = [
    (0, 10, "这是企业采购智能体的实际操作演示。所有资料均为模拟数据，不会产生真实采购或付款。"),
    (
        10,
        32,
        "员工用自然语言描述采购需求。真实大模型自主查询制度、商品目录和部门预算，"
        "再提交采购建议。模型只选择商品和数量，价格与总额由服务端计算。",
    ),
    (
        32,
        59,
        "接下来切换为独立采购审批人。申请人不能审批自己的申请，模型也没有直接下单的权限。"
        "审批人可以查看选型理由、单价、数量和制度依据。确认之前，流程保持等待，部门预算不会扣减。",
    ),
    (59, 69, "服务重启后，同一任务和检查点依然保留，审批可以从保存的位置继续。"),
    (
        69,
        102,
        "批准申请后，工作流从中断处继续执行，登记本地模拟订单。"
        "三台笔记本共两万三千九百九十七元，部门预算由三万元变为六千零三元。"
        "后端在事务中重新校验预算和库存，并通过唯一约束避免重复登记。"
        "执行结果和审批记录可以导出为审计报告。",
    ),
    (
        102,
        123,
        "最后切换至另一个组织，可以看到之前的任务不再可见。组织和角色权限由后端校验。"
        "本项目已通过十二项功能测试，定位是可复现的工程原型，适合用于展示智能体的受控执行能力。",
    ),
]


def run(arguments: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """所有命令使用参数列表；超时或失败时保留已完成的配音分段供续跑。"""
    return subprocess.run(
        arguments,
        check=True,
        cwd=ROOT,
        timeout=timeout,
        text=True,
        encoding="utf-8",
        capture_output=True,
    )


def main() -> None:
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv.exe")
    ffmpeg = shutil.which("ffmpeg") or "C:/env/ffmpeg-7.1.1-essentials_build/bin/ffmpeg.exe"
    ffprobe = str(Path(ffmpeg).with_name("ffprobe.exe")) if ffmpeg.endswith(".exe") else "ffprobe"
    source = ARTIFACTS / "ProcureFlow_演示视频.mp4"
    if not source.is_file():
        raise FileNotFoundError(source)
    narration_dir = ARTIFACTS / "voiceover"
    narration_dir.mkdir(exist_ok=True)
    inputs: list[str] = ["-i", str(source)]
    filters: list[str] = []
    verification: list[dict[str, str | float]] = []
    transcript = ["# 中文配音稿", "", f"音色：{VOICE}；通过免费 Edge 在线语音服务合成。", ""]

    for index, (start, end, text) in enumerate(SEGMENTS):
        audio_path = narration_dir / f"segment-{index + 1}.mp3"
        text_path = narration_dir / f"segment-{index + 1}.txt"
        # 文稿一致且已有完整文件时复用，失败重试不必重复请求其他已完成段落。
        reusable = (
            audio_path.is_file()
            and audio_path.stat().st_size > 1000
            and text_path.is_file()
            and text_path.read_text(encoding="utf-8") == text
        )
        if not reusable:
            print(f"Generating narration {index + 1}/6", flush=True)
            run(
                [
                    uv,
                    "tool",
                    "run",
                    "--from",
                    "edge-tts",
                    "edge-tts",
                    "--voice",
                    VOICE,
                    "--rate=+0%",
                    "--text",
                    text,
                    "--write-media",
                    str(audio_path),
                ]
            )
            text_path.write_text(text, encoding="utf-8")
        result = run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(audio_path),
            ]
        )
        duration = float(json.loads(result.stdout)["format"]["duration"])
        slot = end - start
        # 仅在旁白超过画面时轻微加速，留 0.45 秒段尾停顿，不截断句尾。
        tempo = max(1.0, duration / (slot - 0.45))
        if tempo > 1.25:
            raise ValueError(f"第 {index + 1} 段过长，请缩短文稿后重试：{duration:.1f}s")
        inputs += ["-i", str(audio_path)]
        filters.append(
            f"[{index + 1}:a]atempo={tempo:.6f},aresample=48000,"
            f"adelay={int(start * 1000)}:all=1[a{index}]"
        )
        verification.append(
            {"start": start, "end": end, "raw_duration": duration, "tempo": tempo, "text": text}
        )
        transcript += [f"## {start}–{end} 秒", "", text, ""]

    channels = "".join(f"[a{index}]" for index in range(len(SEGMENTS)))
    filters.append(
        channels + f"amix=inputs={len(SEGMENTS)}:normalize=0,"
        "loudnorm=I=-16:TP=-1.5:LRA=11,apad,atrim=duration=123[voice]"
    )
    output = ARTIFACTS / "ProcureFlow_中文配音版.mp4"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[voice]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-t",
            "123",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
    )
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(output),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "160k",
            "-y",
            str(ARTIFACTS / "ProcureFlow_中文讲解.mp3"),
        ]
    )
    (ARTIFACTS / "中文配音稿.md").write_text("\n".join(transcript), encoding="utf-8")
    (narration_dir / "alignment.json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Narrated video saved: {output}", flush=True)


if __name__ == "__main__":
    main()
