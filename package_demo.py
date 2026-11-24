"""按明确清单打包投递材料，排除密钥、数据库、虚拟环境和本机会话。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


def main() -> None:
    root = Path(__file__).resolve().parent
    destination = root.parent / "ProcureFlow_Agent_源码与演示.zip"
    files = [
        ".gitignore",
        ".python-version",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "启动演示.cmd",
        "演示讲稿.md",
        "app.py",
        "agent.py",
        "domain.py",
        "lab_environment.py",
        "lab_agent.py",
        "run_lab.py",
        "train_lab_policy.py",
        "export_evidence.py",
        "edit_video.py",
        "package_demo.py",
        "add_voiceover.py",
        "artifacts/ProcureFlow_中文配音版.mp4",
        "artifacts/ProcureFlow_中文讲解.mp3",
        "artifacts/中文配音稿.md",
        "artifacts/voiceover/alignment.json",
        "artifacts/ProcureFlow_演示视频.mp4",
        "artifacts/desktop-workspace.png",
        "artifacts/mobile-workspace.png",
        "artifacts/verified-live-run.json",
        "artifacts/采购审计报告.md",
        "artifacts/验证记录.md",
        "artifacts/captions.ass",
        "artifacts/lab/summary.json",
        "artifacts/lab/trajectories.jsonl",
        "artifacts/lab/candidates-train.jsonl",
        "artifacts/lab/candidates-dev.jsonl",
        "artifacts/lab/candidates-test.jsonl",
        "artifacts/lab/policy-learning.json",
        "artifacts/lab-live/summary.json",
        "artifacts/lab-live/trajectories.jsonl",
        "artifacts/lab-live/pre-fix-stock.jsonl",
    ]
    for directory, pattern in [("static", "*"), ("tests", "*.py")]:
        files.extend(
            str(path.relative_to(root))
            for path in (root / directory).glob(pattern)
            if path.is_file()
        )
    missing = [name for name in files if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError(f"交付文件缺失：{', '.join(missing)}")
    # 校验通过后再替换投递包，打包过程失败不影响已有交付。
    with TemporaryDirectory(dir=destination.parent) as temp:
        staged = Path(temp) / destination.name
        with ZipFile(staged, "w", ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(root / name, "enterprise-agent-demo/" + Path(name).as_posix())
        with ZipFile(staged) as archive:
            assert archive.testzip() is None
            assert len(archive.namelist()) == len(files)
            assert not any(
                part in {".env", ".venv", "data", ".session-secret", ".playwright-cli"}
                for name in archive.namelist()
                for part in Path(name).parts
            )
        staged.replace(destination)
    print(f"Package verified: {len(files)} files, {destination.stat().st_size} bytes")


if __name__ == "__main__":
    main()
