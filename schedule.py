#!/usr/bin/env python3
"""Install a per-user daily schedule: macOS launchd, Linux systemd/cron, Windows Task Scheduler."""
import argparse
import datetime as dt
import hashlib
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from radar import ROOT, atomic_write, read_config

LABEL = "com.yanli.product-radar"
NAME = "product-radar"
WINDOWS_TASK = "ProductRadar"
CRON_MARK = "# product-radar:"


def command(root, python):
    return [python, str(root / "radar.py"), "run", "--if-due"]


def same_timezone(config):
    local_offset = dt.datetime.now().astimezone().utcoffset()
    return local_offset == dt.datetime.now(ZoneInfo(config["timezone"])).utcoffset()


def require_timezone(config, system):
    if not same_timezone(config):
        raise SystemExit("%s 按系统时区调度，请先将系统时区设为 %s，或修改 config.json 的 timezone。"
                         % (system, config["timezone"]))


def owned(text, root):
    return str(root / "radar.py") in text


def windows_marker(root):
    # schtasks prints in the OEM code page, so match an ASCII marker rather than a non-ASCII path.
    return "product-radar-root:" + hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]


def run_quiet(args):
    return subprocess.run(args, capture_output=True).returncode == 0


# ---------- macOS: launchd ----------

def spec(root, python, config):
    hour, minute = map(int, config["daily_time"].split(":"))
    return {
        "Label": LABEL,
        "ProgramArguments": command(root, python),
        "WorkingDirectory": str(root),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "TZ": config["timezone"]},
        "StandardOutPath": str(root / "logs/launchd.stdout.log"),
        "StandardErrorPath": str(root / "logs/launchd.stderr.log"),
    }


def macos(action, config):
    target = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    service = "gui/%d/%s" % (os.getuid(), LABEL)
    domain = "gui/%d" % os.getuid()
    if action == "status":
        return subprocess.run(["/bin/launchctl", "print", service]).returncode
    if target.exists() and not owned(" ".join(plistlib.loads(target.read_bytes()).get("ProgramArguments", [])), ROOT):
        raise SystemExit("同名任务指向其他项目，请先核实：" + str(target))
    if action == "uninstall":
        if run_quiet(["/bin/launchctl", "print", service]):
            subprocess.run(["/bin/launchctl", "bootout", service], check=True)
        if target.exists():
            target.unlink()
        return 0
    if action == "install":
        require_timezone(config, "launchd")
    body = plistlib.dumps(spec(ROOT, str(Path(sys.executable).resolve()), config))
    prepared(LABEL + ".plist", body)
    if action == "prepare":
        return 0
    if run_quiet(["/bin/launchctl", "print", service]):
        subprocess.run(["/bin/launchctl", "bootout", service], check=True)
    atomic_write(target, body)
    subprocess.run(["/bin/launchctl", "enable", service], check=True)
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(target)], check=True)
    subprocess.run(["/bin/launchctl", "print", service], check=True, stdout=subprocess.DEVNULL)
    print("已启用 launchd：每天 %s，登录时检查是否需要补跑。\n安装路径：%s" % (config["daily_time"], target))
    return 0


# ---------- Linux: systemd user timer, falling back to cron ----------

def systemd_units(root, python, config):
    def quote(value):
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

    service = "\n".join([
        "[Unit]",
        "Description=Product Radar 创业产品日报",
        "",
        "[Service]",
        "Type=oneshot",
        "WorkingDirectory=" + quote(str(root)),
        "Environment=PYTHONUNBUFFERED=1",
        "ExecStart=" + " ".join(quote(x) for x in command(root, python)),
        "",
    ])
    # The timezone suffix makes systemd fire at config time regardless of the system zone;
    # Persistent=true runs a missed trigger once the user manager starts again.
    timer = "\n".join([
        "[Unit]",
        "Description=Product Radar 每日定时",
        "",
        "[Timer]",
        "OnCalendar=*-*-* %s:00 %s" % (config["daily_time"], config["timezone"]),
        "Persistent=true",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    return service, timer


def cron_lines(root, python, config):
    hour, minute = map(int, config["daily_time"].split(":"))
    job = "cd %s && %s >> %s 2>&1" % (shlex.quote(str(root)), " ".join(shlex.quote(x) for x in command(root, python)),
                                      shlex.quote(str(root / "logs/cron.log")))
    mark = CRON_MARK + str(root)
    return ["%d %d * * * %s %s" % (minute, hour, job, mark), "@reboot sleep 60 && %s %s" % (job, mark)]


def has_systemd_user():
    return shutil.which("systemctl") is not None and run_quiet(["systemctl", "--user", "show-environment"])


def linux_systemd(action, config):
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    service_path, timer_path = unit_dir / (NAME + ".service"), unit_dir / (NAME + ".timer")
    systemctl = ["systemctl", "--user"]
    if action == "status":
        subprocess.run(systemctl + ["list-timers", NAME + ".timer", "--no-pager"])
        return subprocess.run(systemctl + ["status", NAME + ".service", "--no-pager"]).returncode
    if service_path.exists() and not owned(service_path.read_text(encoding="utf-8"), ROOT):
        raise SystemExit("同名 systemd 任务指向其他项目，请先核实：" + str(service_path))
    if action == "uninstall":
        run_quiet(systemctl + ["disable", "--now", NAME + ".timer"])
        for path in (service_path, timer_path):
            if path.exists():
                path.unlink()
        run_quiet(systemctl + ["daemon-reload"])
        return 0
    service, timer = systemd_units(ROOT, sys.executable, config)
    prepared(NAME + ".service", service)
    prepared(NAME + ".timer", timer)
    if action == "prepare":
        return 0
    atomic_write(service_path, service)
    atomic_write(timer_path, timer)
    subprocess.run(systemctl + ["daemon-reload"], check=True)
    subprocess.run(systemctl + ["enable", "--now", NAME + ".timer"], check=True)
    print("已启用 systemd 用户定时器：每天 %s（%s），错过的任务会在下次开机/登录后补跑。"
          % (config["daily_time"], config["timezone"]))
    print("如需在未登录时也运行（服务器场景），执行：loginctl enable-linger $USER")
    return 0


def linux_cron(action, config):
    if not shutil.which("crontab"):
        raise SystemExit("未找到 systemd 用户会话或 crontab，无法自动安装定时任务。")
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = current.stdout.splitlines() if current.returncode == 0 else []
    mark = CRON_MARK + str(ROOT)
    kept = [line for line in existing if not line.endswith(mark)]
    if action == "status":
        mine = [line for line in existing if line.endswith(mark)]
        print("\n".join(mine) if mine else "未安装 cron 定时任务。")
        return 0 if mine else 1
    lines = cron_lines(ROOT, sys.executable, config)
    if action == "prepare":
        prepared(NAME + ".cron", "\n".join(lines) + "\n")
        return 0
    if action == "install":
        require_timezone(config, "cron")
        kept += lines
    subprocess.run(["crontab", "-"], input="".join(line + "\n" for line in kept), text=True, check=True)
    if action == "install":
        print("已写入 crontab：每天 %s（系统时区），开机后补跑一次。" % config["daily_time"])
    return 0


# ---------- Windows: Task Scheduler ----------

def windows_task_xml(root, python, config, user):
    hour, minute = map(int, config["daily_time"].split(":"))
    pythonw = Path(python).with_name("pythonw.exe")
    executable = str(pythonw) if pythonw.exists() else python
    arguments = " ".join('"%s"' % x if " " in x else x for x in command(root, python)[1:])
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Product Radar 创业产品日报 %(marker)s</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T%(hour)02d:%(minute)02d:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>%(user)s</UserId>
      <Delay>PT1M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT4H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(command)s</Command>
      <Arguments>%(arguments)s</Arguments>
      <WorkingDirectory>%(root)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % {"root": escape(str(root)), "hour": hour, "minute": minute, "user": escape(user),
       "command": escape(executable), "arguments": escape(arguments), "marker": windows_marker(root)}


def windows(action, config):
    if action == "status":
        return subprocess.run(["schtasks", "/Query", "/TN", WINDOWS_TASK, "/V", "/FO", "LIST"]).returncode
    current = subprocess.run(["schtasks", "/Query", "/TN", WINDOWS_TASK, "/XML"], capture_output=True)
    if current.returncode == 0 and windows_marker(ROOT).encode() not in current.stdout.replace(b"\x00", b""):
        raise SystemExit("同名计划任务 %s 指向其他项目，请先核实。" % WINDOWS_TASK)
    if action == "uninstall":
        if current.returncode == 0:
            subprocess.run(["schtasks", "/Delete", "/TN", WINDOWS_TASK, "/F"], check=True)
        return 0
    if action == "install":
        require_timezone(config, "Windows 任务计划程序")
    user = "%s\\%s" % (os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", ""))
    path = prepared(WINDOWS_TASK + ".xml", windows_task_xml(ROOT, sys.executable, config, user).encode("utf-16"))
    if action == "prepare":
        return 0
    subprocess.run(["schtasks", "/Create", "/TN", WINDOWS_TASK, "/XML", str(path), "/F"], check=True)
    print("已创建计划任务 %s：每天 %s（系统时区），登录后检查是否需要补跑。" % (WINDOWS_TASK, config["daily_time"]))
    return 0


def prepared(name, body):
    path = ROOT / "scheduler" / name
    atomic_write(path, body)
    print("已生成：" + str(path), flush=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "install", "status", "uninstall"))
    parser.add_argument("--backend", choices=("systemd", "cron"), help="仅 Linux：强制使用某种调度方式")
    args = parser.parse_args()
    config = read_config(ROOT / "config.json")
    (ROOT / "logs").mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        handler = macos
    elif sys.platform == "win32":
        handler = windows
    elif sys.platform.startswith("linux"):
        use_systemd = args.backend == "systemd" or (args.backend is None and has_systemd_user())
        handler = linux_systemd if use_systemd else linux_cron
    else:
        raise SystemExit("暂不支持的系统：" + sys.platform)
    code = handler(args.command, config)
    if args.command == "uninstall" and code == 0:
        print("定时任务已卸载；数据库、日报和原始采集文件保留。")
    return code


if __name__ == "__main__":
    sys.exit(main())
