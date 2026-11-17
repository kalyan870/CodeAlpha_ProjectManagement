import os

from clerk_backend_api import AuthenticateRequestOptions
from clerk_backend_api.security import authenticate_request
from fastapi import HTTPException, Request


def require_user(request: Request) -> str:
    secret_key = os.getenv("CLERK_SECRET_KEY")
    authorized_parties = [value.strip() for value in os.getenv("CLERK_AUTHORIZED_PARTIES", "").split(",") if value.strip()]
    if not secret_key or not authorized_parties:
        raise HTTPException(503, "Clerk server configuration is required")
    state = authenticate_request(
        request,
        AuthenticateRequestOptions(
            secret_key=secret_key,
            jwt_key=os.getenv("CLERK_JWT_KEY") or None,
            authorized_parties=authorized_parties,
            accepts_token=["session_token"],
        ),
    )
    if not state.is_signed_in or not isinstance(state.payload, dict) or not state.payload.get("sub"):
        raise HTTPException(401, "A valid Clerk session is required")
    return str(state.payload["sub"])
