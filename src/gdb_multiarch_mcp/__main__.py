"""Main entry point for gdb-multiarch MCP server."""

import asyncio
from .server import main

if __name__ == "__main__":
    asyncio.run(main())
