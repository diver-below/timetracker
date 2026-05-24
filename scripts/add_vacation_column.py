#!/usr/bin/env python3
"""Add vacation column to current_status table."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import validate_config, DATABASE_URL, logger
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

import ssl

ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE


async def migrate():
    try:
        validate_config()

        # Remove sslmode from URL if present
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(DATABASE_URL)
        query = parse_qs(parsed.query)
        query.pop('sslmode', None)
        new_query = '&'.join(f"{k}={v[0]}" for k, v in query.items())
        db_url = parsed._replace(query=new_query).geturl()

        engine = create_async_engine(
            db_url,
            echo=False,
            connect_args={"ssl": ssl_context},
        )

        async with engine.begin() as conn:
            # Check if column already exists
            result = await conn.execute(text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'current_status'
                AND column_name = 'vacation'
            """))
            exists = result.scalar()

            if not exists:
                logger.info("Adding vacation column to current_status table...")
                await conn.execute(text("""
                    ALTER TABLE current_status
                    ADD COLUMN vacation BOOLEAN NOT NULL DEFAULT FALSE
                """))
                logger.info("Migration completed successfully!")
            else:
                logger.info("Column 'vacation' already exists, no migration needed.")

        await engine.dispose()

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(migrate())