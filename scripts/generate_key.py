#!/usr/bin/env python3
"""Generate an encryption key for the bot."""

from cryptography.fernet import Fernet

key = Fernet.generate_key()
print(f"ENCRYPTION_KEY={key.decode()}")