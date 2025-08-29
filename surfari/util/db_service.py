import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager

import surfari.util.config as config
import surfari.util.surfari_logger as surfari_logger

logger = surfari_logger.getLogger(__name__)

@contextmanager
def get_db_connection_sync():
    path = os.path.join(config.PROJECT_ROOT, "security", "credentials_dev.db")
    if not os.path.exists(path):
        path = os.path.join(config.PROJECT_ROOT, "security", "credentials.db")
    logger.debug(f"Opening DB at {path} (sync)")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # Enables dict(row)
    
    try:
        yield conn
    finally:
        conn.close()

@asynccontextmanager
async def get_db_connection():
    path = os.path.join(config.PROJECT_ROOT, "security", "credentials_dev.db")
    if not os.path.exists(path):
        path = os.path.join(config.PROJECT_ROOT, "security", "credentials.db")
    logger.debug(f"Opening DB at {path} (async)")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row  # Enables dict(row)
    try:
        yield conn
    finally:
        conn.close()
