from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from app.db import create_indexes
from app.routes import releases, reviews, likes, admin, parser, data, health
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mandyfan10-stack.github.io",
        "http://localhost:8888",
        "http://127.0.0.1:8888",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

app.include_router(health.router)
app.include_router(data.router)
app.include_router(releases.router)
app.include_router(reviews.router)
app.include_router(likes.router)
app.include_router(admin.router)
app.include_router(parser.router)

@app.on_event("startup")
async def startup_event():
    await create_indexes()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
