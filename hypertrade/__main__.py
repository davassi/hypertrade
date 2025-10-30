"""Module entrypoint: run the Hypertrade FastAPI app with Uvicorn."""

import uvicorn


def main() -> None:
    """Start Uvicorn pointing at the packaged ASGI app."""
    uvicorn.run(
        "hypertrade.daemon:app",
        host="0.0.0.0",
        port=6487,
        reload=True,
    )


if __name__ == "__main__":
    main()
