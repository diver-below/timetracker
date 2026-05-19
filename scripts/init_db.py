#!/usr/bin/env python3
"""Initialize the database schema."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import validate_config
from db import init_db


async def main():
    try:
        validate_config()
        await init_db()
        print("Database initialized successfully!")
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())