#!/usr/bin/env python3
"""Migrate database schema from BigInteger to String for yandex_user_login."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import validate_config, DATABASE_URL, logger
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
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
            # Check if column type needs changing
            result = await conn.execute(text("""
                SELECT data_type
                FROM information_schema.columns
                WHERE table_name = 'users'
                AND column_name = 'yandex_user_login'
            """))
            current_type = result.scalar()

            if current_type == 'bigint':
                logger.info("Migrating yandex_user_login from bigint to varchar(255)...")
                await conn.execute(text("""
                    ALTER TABLE users
                    ALTER COLUMN yandex_user_login TYPE VARCHAR(255)
                """))
                logger.info("Migration completed successfully!")
            elif current_type == 'character varying':
                logger.info("Column is already VARCHAR(255), no migration needed.")
            else:
                logger.warning(f"Unexpected column type: {current_type}")

        await engine.dispose()

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(migrate())