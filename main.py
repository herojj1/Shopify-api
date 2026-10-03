"""
Railpack entrypoint — re-exports the FastAPI app from api_server so
uvicorn's auto-detection finds it.
"""
import os
import uvicorn

from api_server import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
        backlog=2048,
        limit_concurrency=int(os.environ.get("CHECKER_THREADS", "200")) * 2,
        timeout_keep_alive=60,
    )
