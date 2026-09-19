import uvicorn

from app.server import create_app

app = create_app(None, 8600)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8600, log_level="warning")
