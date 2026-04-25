from fastapi import HTTPException, Request
from collections import defaultdict
import time

class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self.clients = defaultdict(list)

    async def __call__(self, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        self.clients[client_ip] = [t for t in self.clients[client_ip] if now - t < 60]
        if len(self.clients[client_ip]) >= self.requests_per_minute:
            raise HTTPException(status_code=429, detail="Too many requests")
        self.clients[client_ip].append(now)

rate_limiter = RateLimiter(requests_per_minute=20)
