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

    # Persist User and GitHubConnection into SQLite store via atomic upsert
    sqlite_store = getattr(request.app.state, "sqlite_store", None)
    if sqlite_store:
        user, _ = sqlite_store.upsert_github_login(
            github_user_id=str(github_user["id"]),
            username=github_user["login"],
            email=github_user.get("email"),
            avatar_url=github_user.get("avatar_url"),
            access_token=token["access_token"],
            token_type=token.get("token_type", "Bearer"),
            scope=token.get("scope"),
        )
        user_id = user.id
    else:
        user_id = f"usr_{github_user['id']}"

    jwt_token = create_access_token({
        "sub": user_id,  # application User.id
        "username": github_user.get("login"),
        "avatar_url": github_user.get("avatar_url"),
        "email": github_user.get("email"),
    })

    redirect_url = f"{FRONTEND_URL}/auth/success?token={jwt_token}"

    return RedirectResponse(url=redirect_url)