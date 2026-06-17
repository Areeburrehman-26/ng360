#!/usr/bin/env python3
"""
ng360_bot_taskbar.py
--------------------
macOS Taskbar App for NG360 Bot Control

Features:
  - Pause/Resume bot processing
  - Restart bot services
  - View today's quote status
  - Resort queue by GHL date created
  - Real-time status updates

Requirements:
  pip install rumps requests
"""

import rumps
import requests
import json
import subprocess
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_DIR          = Path("/Users/desmondthomas/Desktop/all-in-one/nsg360_bot")
QUEUE_FILE       = BOT_DIR / "data" / "ng360_queue.json"
WEBHOOK_URL      = "http://localhost:8004"
REFRESH_INTERVAL = 10  # seconds


# ---------------------------------------------------------------------------
# Bot Controller
# ---------------------------------------------------------------------------

class NG360BotController:
    """Controls the NG360 Bot webhook server and worker processes."""

    def __init__(self):
        self.paused = False

    def get_webhook_pid(self) -> Optional[int]:
        """Get the PID of the NG360 webhook server process."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "nsg360_bot/core/webhook_server"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split()[0])
        except Exception:
            pass
        # Fallback: match on port 8004 binding
        try:
            result = subprocess.run(
                ["lsof", "-ti", ":8004"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split()[0])
        except Exception:
            pass
        return None

    def get_worker_pid(self) -> Optional[int]:
        """Get the PID of the NG360 worker process."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "nsg360_bot/core/worker"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split()[0])
        except Exception:
            pass
        return None

    def get_chrome_pids(self) -> list[int]:
        """Get PIDs of Chrome processes launched by the NG360 bot."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "chrome.*remote-debugging-port"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                return [int(pid) for pid in result.stdout.strip().split("\n")]
        except Exception:
            pass
        return []

    def is_bot_running(self) -> bool:
        """Check if the bot is running by hitting its health endpoint."""
        try:
            response = requests.get(f"{WEBHOOK_URL}/health", timeout=2)
            return response.status_code == 200
        except Exception:
            return False

    def get_queue_status(self) -> Optional[dict]:
        """Get queue status from the webhook server."""
        try:
            response = requests.get(f"{WEBHOOK_URL}/queue", timeout=2)
            if response.status_code == 200:
                return response.json()
        except Exception:
            pass
        return None

    def pause_bot(self) -> bool:
        """Pause bot processing (SIGSTOP the webhook server)."""
        webhook_pid = self.get_webhook_pid()
        if webhook_pid:
            try:
                os.kill(webhook_pid, signal.SIGSTOP)
                self.paused = True
                return True
            except Exception as e:
                print(f"Failed to pause bot: {e}")
        return False

    def resume_bot(self) -> bool:
        """Resume bot processing (SIGCONT the webhook server)."""
        webhook_pid = self.get_webhook_pid()
        if webhook_pid:
            try:
                os.kill(webhook_pid, signal.SIGCONT)
                self.paused = False
                return True
            except Exception as e:
                print(f"Failed to resume bot: {e}")
        return False

    def restart_bot(self) -> bool:
        """Restart the bot services (webhook + worker + Chrome)."""
        try:
            webhook_pid = self.get_webhook_pid()
            if webhook_pid:
                os.kill(webhook_pid, signal.SIGTERM)

            worker_pid = self.get_worker_pid()
            if worker_pid:
                os.kill(worker_pid, signal.SIGTERM)

            for chrome_pid in self.get_chrome_pids():
                try:
                    os.kill(chrome_pid, signal.SIGTERM)
                except Exception:
                    pass

            import time
            time.sleep(2)

            subprocess.Popen(
                ["bash", str(BOT_DIR / "start_bot.sh"), "--skip-tests"],
                cwd=str(BOT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            self.paused = False
            return True
        except Exception as e:
            print(f"Failed to restart bot: {e}")
            return False

    def restart_webhook_only(self) -> bool:
        """Restart only the webhook server (leaves worker running)."""
        try:
            webhook_pid = self.get_webhook_pid()
            if webhook_pid:
                os.kill(webhook_pid, signal.SIGTERM)

            import time
            time.sleep(1)

            subprocess.Popen(
                ["bash", str(BOT_DIR / "start_bot.sh"), "--skip-tests"],
                cwd=str(BOT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            self.paused = False
            return True
        except Exception as e:
            print(f"Failed to restart webhook: {e}")
            return False

    def get_todays_quotes(self) -> dict:
        """Get summary of today's quotes from the queue file."""
        if not QUEUE_FILE.exists():
            return {"completed": 0, "failed": 0, "pending": 0, "processing": 0}

        try:
            with open(QUEUE_FILE) as f:
                queue_data = json.load(f)

            today = datetime.now(timezone.utc).date()
            counts = {"completed": 0, "failed": 0, "pending": 0, "processing": 0}

            for job in queue_data:
                job_date = datetime.fromisoformat(
                    job.get("created_at", "").replace("Z", "+00:00")
                ).date()

                if job_date == today:
                    status = job.get("status", "").lower()
                    if status in counts:
                        counts[status] += 1

            return counts
        except Exception as e:
            print(f"Failed to get today's quotes: {e}")
            return {"completed": 0, "failed": 0, "pending": 0, "processing": 0}

    def resort_queue_by_ghl_date(self) -> bool:
        """Resort pending jobs in the queue by newest first."""
        if not QUEUE_FILE.exists():
            return False

        try:
            with open(QUEUE_FILE) as f:
                queue_data = json.load(f)

            pending_jobs = [j for j in queue_data if j.get("status") == "PENDING"]
            other_jobs   = [j for j in queue_data if j.get("status") != "PENDING"]

            pending_jobs.sort(
                key=lambda j: j.get("created_at", ""),
                reverse=True,
            )

            new_queue = other_jobs + pending_jobs

            with open(QUEUE_FILE, "w") as f:
                json.dump(new_queue, f, indent=2)

            return True
        except Exception as e:
            print(f"Failed to resort queue: {e}")
            return False


# ---------------------------------------------------------------------------
# Taskbar App
# ---------------------------------------------------------------------------

class NG360BotApp(rumps.App):
    """macOS Taskbar App for NG360 Bot."""

    def __init__(self):
        super().__init__("NG360", quit_button=None)
        self.controller = NG360BotController()
        self.update_menu()

    def update_menu(self):
        """Refresh the menu with current status."""
        self.menu.clear()

        is_running  = self.controller.is_bot_running()
        status_icon = "🟢" if is_running else "🔴"
        pause_icon  = "⏸️" if self.controller.paused else ""

        status_text = f"{status_icon} NG360: {'Running' if is_running else 'Stopped'} {pause_icon}"
        self.menu.add(rumps.MenuItem(status_text, callback=None))
        self.menu.add(rumps.separator)

        queue_status = self.controller.get_queue_status()
        if queue_status:
            pending    = queue_status.get("pending", 0)
            processing = queue_status.get("processing", 0)
            completed  = queue_status.get("completed", 0)
            failed     = queue_status.get("failed", 0)

            self.menu.add(rumps.MenuItem("📊 Queue Status", callback=None))
            self.menu.add(rumps.MenuItem(f"   Pending: {pending}", callback=None))
            self.menu.add(rumps.MenuItem(f"   Processing: {processing}", callback=None))
            self.menu.add(rumps.MenuItem(f"   Completed: {completed}", callback=None))
            self.menu.add(rumps.MenuItem(f"   Failed: {failed}", callback=None))
        else:
            self.menu.add(rumps.MenuItem("📊 Queue: Unavailable", callback=None))

        self.menu.add(rumps.separator)

        todays = self.controller.get_todays_quotes()
        self.menu.add(rumps.MenuItem("📅 Today's Quotes", callback=None))
        self.menu.add(rumps.MenuItem(f"   ✅ Completed: {todays['completed']}", callback=None))
        self.menu.add(rumps.MenuItem(f"   ❌ Failed: {todays['failed']}", callback=None))
        self.menu.add(rumps.MenuItem(f"   ⏳ Pending: {todays['pending']}", callback=None))

        self.menu.add(rumps.separator)

        if is_running:
            if self.controller.paused:
                self.menu.add(rumps.MenuItem("▶️ Resume Bot", callback=self.resume_bot))
            else:
                self.menu.add(rumps.MenuItem("⏸️ Pause Bot", callback=self.pause_bot))

            self.menu.add(rumps.MenuItem("🔄 Restart Bot (Full)", callback=self.restart_bot))
            self.menu.add(rumps.MenuItem("🔄 Restart Webhook Server", callback=self.restart_webhook))
        else:
            self.menu.add(rumps.MenuItem("▶️ Start Bot", callback=self.start_bot))

        self.menu.add(rumps.MenuItem("🔃 Resort Queue (Newest First)", callback=self.resort_queue))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("🔄 Refresh Status", callback=self.refresh_status))
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("❌ Quit", callback=self.quit_app))

    @rumps.clicked("⏸️ Pause Bot")
    def pause_bot(self, _):
        if self.controller.pause_bot():
            rumps.notification(
                "NG360 Bot", "Bot Paused",
                "The bot will not accept new jobs until resumed."
            )
        else:
            rumps.alert("Failed to pause bot")
        self.update_menu()

    @rumps.clicked("▶️ Resume Bot")
    def resume_bot(self, _):
        if self.controller.resume_bot():
            rumps.notification(
                "NG360 Bot", "Bot Resumed",
                "The bot is now accepting new jobs."
            )
        else:
            rumps.alert("Failed to resume bot")
        self.update_menu()

    @rumps.clicked("🔄 Restart Bot (Full)")
    def restart_bot(self, _):
        response = rumps.alert(
            "Restart NG360 Bot?",
            "This will stop the current job and restart all services.",
            ok="Restart",
            cancel="Cancel",
        )
        if response == 1:
            if self.controller.restart_bot():
                rumps.notification(
                    "NG360 Bot", "Bot Restarted",
                    "All services restarted successfully."
                )
            else:
                rumps.alert("Failed to restart bot")
        self.update_menu()

    @rumps.clicked("🔄 Restart Webhook Server")
    def restart_webhook(self, _):
        response = rumps.alert(
            "Restart Webhook Server?",
            "This will restart only the webhook server (worker keeps running).",
            ok="Restart",
            cancel="Cancel",
        )
        if response == 1:
            if self.controller.restart_webhook_only():
                rumps.notification(
                    "NG360 Bot", "Webhook Restarted",
                    "Webhook server restarted successfully."
                )
            else:
                rumps.alert("Failed to restart webhook server")
        self.update_menu()

    @rumps.clicked("▶️ Start Bot")
    def start_bot(self, _):
        try:
            subprocess.Popen(
                ["bash", str(BOT_DIR / "start_bot.sh"), "--skip-tests"],
                cwd=str(BOT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rumps.notification(
                "NG360 Bot", "Bot Starting",
                "Webhook server is starting..."
            )
        except Exception as e:
            rumps.alert(f"Failed to start bot: {e}")

        import time
        time.sleep(3)
        self.update_menu()

    @rumps.clicked("🔃 Resort Queue (Newest First)")
    def resort_queue(self, _):
        response = rumps.alert(
            "Resort Queue?",
            "This will reorder pending jobs by newest first.",
            ok="Resort",
            cancel="Cancel",
        )
        if response == 1:
            if self.controller.resort_queue_by_ghl_date():
                rumps.notification(
                    "NG360 Bot", "Queue Resorted",
                    "Pending jobs reordered by newest first."
                )
            else:
                rumps.alert("Failed to resort queue")
        self.update_menu()

    @rumps.clicked("🔄 Refresh Status")
    def refresh_status(self, _):
        self.update_menu()

    @rumps.clicked("❌ Quit")
    def quit_app(self, _):
        rumps.quit_application()

    @rumps.timer(REFRESH_INTERVAL)
    def auto_refresh(self, _):
        """Auto-refresh menu every 10 seconds."""
        self.update_menu()


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = NG360BotApp()
    app.run()
