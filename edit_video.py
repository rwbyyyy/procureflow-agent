"""剪辑本次真实操作录屏，保留动作顺序并注明模拟与节选边界。"""

import shutil
import subprocess
from pathlib import Path


def main() -> None:
    binary = shutil.which("ffmpeg") or "C:/env/ffmpeg-7.1.1-essentials_build/bin/ffmpeg.exe"
    source = Path("artifacts/procureflow-demo.webm")
    if not source.is_file():
        raise FileNotFoundError("需要先提供本次原始实录 artifacts/procureflow-demo.webm")
    segments = [(38, 70), (130, 157), (230, 240), (310, 343), (415, 436)]
    filters = [
        f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]"
        for i, (start, end) in enumerate(segments)
    ]
    inputs = "".join(f"[v{i}]" for i in range(len(segments)))
    filters.append(
        inputs + f"concat=n={len(segments)}:v=1:a=0,fps=20,"
        "pad=1600:1080:0:0:color=0x142b2a,"
        "ass=artifacts/captions.ass[out]"
    )
    subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(source),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-y",
            "artifacts/ProcureFlow_演示视频.mp4",
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
