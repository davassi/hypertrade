import uvicorn

# Import the FastAPI app instance
def main() -> None:
    uvicorn.run(
        "hypertrade.daemon:app",
        host="0.0.0.0",
        port=9414,
        reload=True,
    )

if __name__ == "__main__":
    main()
