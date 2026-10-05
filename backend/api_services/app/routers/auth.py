from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from authlib.integrations.starlette_client import OAuth
from starlette.requests import Request
import httpx

from api_services.app.config import (
    GITHUB_CLIENT_ID,
    GITHUB_CLIENT_SECRET,
    FRONTEND_URL,
)
from api_services.app.utils.jwt_utils import create_access_token

router = APIRouter()

oauth = OAuth()

oauth.register(
    name="github",
    client_id=GITHUB_CLIENT_ID,
    client_secret=GITHUB_CLIENT_SECRET,
    access_token_url="https://github.com/login/oauth/access_token",
    authorize_url="https://github.com/login/oauth/authorize",
    api_base_url="https://api.github.com/",
    client_kwargs={"scope": "user:email"},
)

@router.get("/github/login")
async def github_login(request: Request):
    redirect_uri = request.url_for("github_callback")
    return await oauth.github.authorize_redirect(request, redirect_uri)

@router.get("/github/callback")
async def github_callback(request: Request):
    token = await oauth.github.authorize_access_token(request)

    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )

    github_user = user_resp.json()
    # print(github_user)

    jwt_token = create_access_token({
        "sub": str(github_user["id"]),
        "username": github_user["login"],
        "avatar_url": github_user["avatar_url"],
        "email": github_user["email"],
    })

    redirect_url = (
        f"{FRONTEND_URL}/auth/success"
        f"?token={jwt_token}"
    )

    return RedirectResponse(url=redirect_url)