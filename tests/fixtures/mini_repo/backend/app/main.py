from fastapi import FastAPI

from .api import auth, claims

app = FastAPI(title="ClaimIQ")
app.include_router(auth.router)
app.include_router(claims.router)
