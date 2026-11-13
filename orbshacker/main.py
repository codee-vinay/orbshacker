"""
main.py – Application entry point and main loop.
"""

import os
import sys
import subprocess
import time

from . import config
from .ui import (
    Colors,
    _colors,
    print_color,
    print_banner,
    print_menu,
    print_selected,
    show_credits,
)
from .faker import GameFaker, manual_mode
from .discord_db import DiscordGamesDB, database_mode
from .steam import steam_quest_mode
from .updater import auto_update
from .errors import DatabaseLoadError


def _show_settings_warnings() -> None:
    """Print configuration warnings collected at import, without blocking startup."""
    try:
        for message in config.SETTINGS_WARNINGS:
            print_color(message, Colors.YELLOW)
    except Exception:
        # A display problem must never prevent the application from starting.
        pass


def main() -> None:
    """Main application loop."""
    # Report configuration problems before the updater can exit or change flow.
    _show_settings_warnings()

    try:
        auto_update()
    except Exception:
        pass

    print_banner()
    print_color("Initializing orbshacker...", Colors.CYAN)
    print_color("[*] Connecting to Discord API...", Colors.GRAY)

    try:
        db = DiscordGamesDB()
        db.ensure_loaded()
    except DatabaseLoadError as e:
        print_color(f"[ERROR] {e}", Colors.RED, bold=True)
        sys.exit(1)

    faker = GameFaker()
    print_color("[OK] Ready to fake some games!", Colors.GREEN)
    time.sleep(0.5)

    try:
        while True:
            try:
                subprocess.run(
                    'cls' if os.name == 'nt' else 'clear',
                    shell=True,
                    check=False,
                )
                print_banner()

                if db.source:
                    print_color(f"   Active Database: {db.source} ({len(db.games)} games)", Colors.GRAY)

                print_menu()
                C = _colors()
                choice = input(
                    f"{C.BOLD}{C.ACCENT}Select option{C.RESET}"
                    f" {C.MUTED}[1-5]{C.RESET}: "
                ).strip()
                print_selected(choice)

                if choice == '1':
                    database_mode(db, faker)
                elif choice == '2':
                    manual_mode(faker)
                elif choice == '3':
                    steam_quest_mode(faker)
                elif choice == '4':
                    show_credits()
                elif choice == '5':
                    print_color(f"\n[*] Thanks for using orbshacker!", Colors.CYAN, bold=True)
                    print_color(f"[*] Developed by {config.DEVELOPER}", Colors.GRAY)
                    print_color("\n[*] May your orbs be plentiful!", Colors.MAGENTA)
                    break
                else:
                    print_color("\n[ERROR] Invalid option - try 1, 2, 3, 4 or 5", Colors.RED)
                    time.sleep(config.SLEEP_SHORT)

            except KeyboardInterrupt:
                print_color("\n\n[!] Interrupted by user", Colors.YELLOW)
                print_color("[*] Thanks for using orbshacker!\n", Colors.CYAN)
                break
    finally:
        faker.cleanup()
