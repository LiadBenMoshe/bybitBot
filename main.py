from __future__ import annotations

from config import get_settings
from logger import configure_logging
from ui import render_dashboard


def main() -> None:
    settings = get_settings()
    configure_logging(settings.event_log_path)
    render_dashboard()


if __name__ == "__main__":
    main()
