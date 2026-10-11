#!/usr/bin/env python3
"""Install a per-user macOS schedule, independent of the Codex application."""
import argparse
import datetime as dt
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from zoneinfo import ZoneInfo

from radar import ROOT, atomic_write, read_config

LABEL = "com.yanli.product-radar"


def spec(root, python, config):
    hour, minute = map(int, config["daily_time"].split(":"))
    return {
        "Label": LABEL,
        "ProgramArguments": [python, str(root / "radar.py"), "run", "--if-due"],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "TZ": config["timezone"]},
        "StandardOutPath": str(root / "logs/launchd.stdout.log"),
        "StandardErrorPath": str(root / "logs/launchd.stderr.log"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "install", "status", "uninstall"))
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("此安装器适用于 macOS；Linux 可直接用系统定时器调用 radar.py run。")
    config = read_config(ROOT / "config.json")
    target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    service = "gui/%d/%s" % (os.getuid(), LABEL)
    domain = "gui/%d" % os.getuid()
    if args.command == "status":
        return subprocess.run(["/bin/launchctl", "print", service]).returncode
    if args.command == "uninstall":
        if target.exists():
            existing = plistlib.loads(target.read_bytes())
            if existing.get("Label") != LABEL or str(ROOT / "radar.py") not in existing.get("ProgramArguments", []):
                raise SystemExit("同名任务不属于当前项目，未删除。")
        loaded = subprocess.run(["/bin/launchctl", "print", service], capture_output=True).returncode == 0
        if loaded:
            subprocess.run(["/bin/launchctl", "bootout", service], check=True)
        if target.exists():
            target.unlink()
        print("定时任务已卸载；数据库、日报和原始采集文件保留。")
        return 0
    if args.command == "install":
        local_offset = dt.datetime.now().astimezone().utcoffset()
        wanted_offset = dt.datetime.now(ZoneInfo(config["timezone"])).utcoffset()
        if local_offset != wanted_offset:
            raise SystemExit("launchd 按系统时区调度，请先将 macOS 时区设为 " + config["timezone"])
    body = plistlib.dumps(spec(ROOT, str(Path(sys.executable).resolve()), config))
    prepared = ROOT / "scheduler" / (LABEL + ".plist")
    atomic_write(prepared, body)
    print("已生成：" + str(prepared), flush=True)
    if args.command == "prepare":
        return 0
    # Check ownership before replacing an existing same-name service.
    if target.exists():
        existing = plistlib.loads(target.read_bytes())
        if existing.get("Label") != LABEL or str(ROOT / "radar.py") not in existing.get("ProgramArguments", []):
            raise SystemExit("同名任务指向其他项目，请先核实后再安装。")
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    loaded = subprocess.run(["/bin/launchctl", "print", service], capture_output=True).returncode == 0
    if loaded:
        subprocess.run(["/bin/launchctl", "bootout", service], check=True)
    atomic_write(target, body)
    subprocess.run(["/bin/launchctl", "enable", service], check=True)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(target)], check=True)
    subprocess.run(["/bin/launchctl", "print", service], check=True, stdout=subprocess.DEVNULL)
    print("已启用：每天 %s（系统时区），用户登录时检查是否需要补跑。" % config["daily_time"])
    print("安装路径：" + str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
