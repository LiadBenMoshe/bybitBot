from __future__ import annotations

import uvicorn

from webapp import SETTINGS, app


if __name__ == "__main__":
    uvicorn.run(app, host=SETTINGS.app_host, port=SETTINGS.app_port)
