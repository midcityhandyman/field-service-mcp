"""Run the mock vendor API as a real HTTP server: python -m field_service_mcp.mock_api"""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    from .app import create_app

    host = os.environ.get("MOCK_API_HOST", "127.0.0.1")
    port = int(os.environ.get("MOCK_API_PORT", "8765"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
