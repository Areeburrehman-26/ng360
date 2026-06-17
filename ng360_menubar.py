#!/usr/bin/env python3
"""
ng360_menubar.py
NG360 Bot Menu Bar Controller

A macOS menu bar application to control and monitor the NG360 Bot.

Features:
- Start/Stop/Restart the bot
- Pause/Resume processing
- View real-time stats (quotes today, success rate, queue size)
- Kill stuck processes
- View recent activity
"""

import rumps
import subprocess
import os
import signal
import time
from pathlib import Path
from datetime import datetime
import json


class NG360BotController(rumps.App):
    def __init__(self):
        super(NG360BotController, self).__init__(
            "NG360",
            icon=None,
            quit_button=None,
        )

        self.bot_dir       = Path("/Users/desmondthomas/Desktop/all-in-one/nsg360_bot")
        self.worker_script = self.bot_dir / "core" / "worker.py"
        self.log_file      = self.bot_dir / "logs" / "worker.log"
        self.queue_file    = self.bot_dir / "data" / "ng360_queue.json"
        self.pid_file      = self.bot_dir / "worker.pid"

        self.bot_process = None
        self.is_paused   = False

        self.menu = [
            rumps.MenuItem("Status: Checking...", callback=None),
            rumps.separator,
            rumps.MenuItem("Start Bot",    callback=self.start_bot),
            rumps.MenuItem("Stop Bot",     callback=self.stop_bot),
            rumps.MenuItem("Restart Bot",  callback=self.restart_bot),
            rumps.separator,
            rumps.MenuItem("Pause Processing", callback=self.toggle_pause),
            rumps.separator,
            rumps.MenuItem("Stats", callback=None),
            rumps.MenuItem("  Quotes Today: 0",  callback=None),
            rumps.MenuItem("  Success Rate: 0%", callback=None),
            rumps.MenuItem("  Queue Size: 0",    callback=None),
            rumps.separator,
            rumps.MenuItem("Kill All NG360 Processes", callback=self.kill_all),
            rumps.separator,
            rumps.MenuItem("Open Logs Folder",    callback=self.open_logs),
            rumps.MenuItem("View Recent Activity", callback=self.view_activity),
            rumps.separator,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        self.timer = rumps.Timer(self.update_status, 5)
        self.timer.start()
        self.update_status(None)

    # ── Process detection ──────────────────────────────────

    def get_bot_pid(self):
        """Get the PID of the running NG360 worker process."""
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                os.kill(pid, 0)
                return pid
            except (ValueError, ProcessLookupError, OSError):
                self.pid_file.unlink(missing_ok=True)
                return None
        return None

    def is_bot_running(self):
        """Check if the NG360 bot worker is running."""
        pid = self.get_bot_pid()
        if pid:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        try:
            result = subprocess.run(
                ["pgrep", "-f", "nsg360_bot/core/worker"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    # ── Stats ──────────────────────────────────────────────

    def get_queue_size(self) -> int:
        """Count pending jobs in the queue file."""
        if not self.queue_file.exists():
            return 0
        try:
            data = json.loads(self.queue_file.read_text())
            return sum(1 for j in data if j.get("status") == "PENDING")
        except Exception:
            return 0

    def get_stats(self):
        stats = {
            "quotes_today": 0,
            "success_rate": 0,
            "queue_size": self.get_queue_size(),
            "last_quote": "Never",
        }

        if not self.log_file.exists():
            return stats

        try:
            today = datetime.now().strftime("%Y-%m-%d")
            with open(self.log_file) as f:
                lines = f.readlines()

            successful  = 0
            failed      = 0
            last_quote_time = None

            for line in reversed(lines[-1000:]):
                if today in line:
                    if "QUOTE COMPLETE:" in line:
                        successful += 1
                        if not last_quote_time:
                            parts = line.split()
                            if len(parts) >= 2:
                                last_quote_time = f"{parts[0]} {parts[1]}"
                    elif "Quote failed" in line:
                        failed += 1

            stats["quotes_today"] = successful
            if successful + failed > 0:
                stats["success_rate"] = int(successful / (successful + failed) * 100)
            if last_quote_time:
                stats["last_quote"] = last_quote_time.split()[1][:8]

        except Exception as e:
            print(f"Error getting stats: {e}")

        return stats

    # ── Status updates ─────────────────────────────────────

    def update_status(self, _):
        running = self.is_bot_running()
        stats   = self.get_stats()

        if running:
            if self.is_paused:
                self.menu["Status: Checking..."].title = "Status: ⏸ Paused"
                self.title = "NG360 ⏸"
            else:
                self.menu["Status: Checking..."].title = "Status: ✅ Running"
                self.title = "NG360 ✅"
        else:
            self.menu["Status: Checking..."].title = "Status: ⭕ Stopped"
            self.title = "NG360 ⭕"

        self.menu["  Quotes Today: 0"].title  = f"  Quotes Today: {stats['quotes_today']}"
        self.menu["  Success Rate: 0%"].title = f"  Success Rate: {stats['success_rate']}%"
        self.menu["  Queue Size: 0"].title    = f"  Queue Pending: {stats['queue_size']}"

        if self.is_paused:
            self.menu["Pause Processing"].title = "Resume Processing"
        else:
            self.menu["Pause Processing"].title = "Pause Processing"

    # ── Bot control ────────────────────────────────────────

    def start_bot(self, _):
        """Start the NG360 bot via start_bot.sh."""
        if self.is_bot_running():
            rumps.alert("Bot Already Running", "The NG360 bot is already running.")
            return

        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(self.bot_dir)

            subprocess.Popen(
                ["bash", str(self.bot_dir / "start_bot.sh"), "--skip-tests"],
                cwd=str(self.bot_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            time.sleep(3)

            if self.is_bot_running():
                rumps.notification(
                    title="NG360 Bot Started",
                    subtitle="Worker is now processing GA quotes",
                    message=f"PID: {self.get_bot_pid()}",
                )
            else:
                rumps.alert("Start Failed", "Bot process started but is not running. Check logs.")

        except Exception as e:
            rumps.alert("Error Starting Bot", str(e))

    def stop_bot(self, _):
        """Stop the NG360 bot worker gracefully."""
        pid = self.get_bot_pid()
        if not pid:
            rumps.alert("Bot Not Running", "The NG360 bot is not currently running.")
            return

        try:
            os.kill(pid, signal.SIGTERM)

            for _ in range(10):
                time.sleep(1)
                if not self.is_bot_running():
                    break

            if self.is_bot_running():
                os.kill(pid, signal.SIGKILL)

            self.pid_file.unlink(missing_ok=True)

            rumps.notification(
                title="NG360 Bot Stopped",
                subtitle="Worker has been shut down",
                message="",
            )

        except Exception as e:
            rumps.alert("Error Stopping Bot", str(e))

    def restart_bot(self, _):
        """Restart the NG360 bot."""
        if self.is_bot_running():
            self.stop_bot(_)
            time.sleep(2)
        self.start_bot(_)

    def toggle_pause(self, _):
        """Pause or resume quote processing."""
        if not self.is_bot_running():
            rumps.alert("Bot Not Running", "Start the bot first before pausing.")
            return

        pid = self.get_bot_pid()
        if not pid:
            return

        try:
            if self.is_paused:
                os.kill(pid, signal.SIGCONT)
                self.is_paused = False
                rumps.notification(
                    title="Processing Resumed",
                    subtitle="NG360 Bot is now processing quotes",
                    message="",
                )
            else:
                os.kill(pid, signal.SIGSTOP)
                self.is_paused = True
                rumps.notification(
                    title="Processing Paused",
                    subtitle="Bot will not process new quotes",
                    message="Click Resume to continue",
                )
        except Exception as e:
            rumps.alert("Error", str(e))

    def kill_all(self, _):
        """Force-kill all NG360 bot related processes."""
        response = rumps.alert(
            title="Kill All Processes?",
            message="This will force-kill all NG360 bot processes. Use only if the bot is stuck.",
            ok="Kill All",
            cancel="Cancel",
        )

        if response != 1:
            return

        try:
            subprocess.run(["pkill", "-9", "-f", "nsg360_bot/core/worker"],       timeout=5)
            subprocess.run(["pkill", "-9", "-f", "nsg360_bot/core/webhook_server"], timeout=5)
            subprocess.run(["pkill", "-9", "-f", "nsg360_bot/core/bridge_bot"],    timeout=5)

            self.pid_file.unlink(missing_ok=True)
            self.is_paused = False

            rumps.notification(
                title="Processes Killed",
                subtitle="All NG360 bot processes terminated",
                message="You can now start the bot again",
            )

        except Exception as e:
            rumps.alert("Error", str(e))

    # ── Utilities ──────────────────────────────────────────

    def open_logs(self, _):
        """Open the NG360 logs folder in Finder."""
        subprocess.run(["open", str(self.bot_dir / "logs")])

    def view_activity(self, _):
        """Show recent quote activity in an alert."""
        if not self.log_file.exists():
            rumps.alert("No Logs", "Log file not found.")
            return

        try:
            with open(self.log_file) as f:
                lines = f.readlines()

            recent = []
            for line in reversed(lines[-500:]):
                if "QUOTE COMPLETE:" in line:
                    parts = line.split("QUOTE COMPLETE:")
                    if len(parts) == 2:
                        time_part = parts[0].split()[1]
                        name = parts[1].strip()
                        recent.append(f"✅ {time_part[:8]} - {name}")
                        if len(recent) >= 10:
                            break
                elif "Quote failed" in line and "contact" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        recent.append(f"❌ {parts[1][:8]} - Failed")
                        if len(recent) >= 10:
                            break

            message = "\n".join(reversed(recent)) if recent else "No recent activity found"
            rumps.alert(title="Recent NG360 Activity", message=message)

        except Exception as e:
            rumps.alert("Error", str(e))


if __name__ == "__main__":
    app = NG360BotController()
    app.run()
