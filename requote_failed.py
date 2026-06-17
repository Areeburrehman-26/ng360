#!/usr/bin/env python3
"""
requote_failed.py
-----------------
Alias entry-point for the NG360 Bot menu bar controller.
Equivalent to running ng360_menubar.py directly.

Usage:
    python3 requote_failed.py
"""

from ng360_menubar import NG360BotController

if __name__ == "__main__":
    app = NG360BotController()
    app.run()
