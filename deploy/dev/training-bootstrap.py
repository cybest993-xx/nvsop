"""在原训练 app 进程补 DB readiness；不改变基座 handlers 或 startup。"""

import importlib
import os
from pathlib import Path

from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

password = Path(os.environ["POSTGRES_PASSWORD_FILE"]).read_text().strip()
if not password:
    raise ValueError("training runtime secret is empty")
os.environ["POSTGRES_DB_URL"] = URL.create(
    "postgresql+asyncpg",
    username=os.environ["POSTGRES_USER"],
    password=password,
    host=os.environ["POSTGRES_HOST"],
    port=int(os.environ.get("POSTGRES_PORT", "5432")),
    database=os.environ["POSTGRES_DB"],
).render_as_string(hide_password=False)

app = importlib.import_module("app").app
postgres_db = importlib.import_module("components.postgres_db").postgres_db


@app.get("/health/ready", include_in_schema=False)
async def ready() -> JSONResponse:
    try:
        async with postgres_db.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return JSONResponse({"ready": False}, status_code=503)
    return JSONResponse({"ready": True})
