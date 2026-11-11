import argparse

import fastapi
import uvicorn

app = fastapi.FastAPI(title="revtrend")


def main():
    parser = argparse.ArgumentParser(prog="revtrend-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
