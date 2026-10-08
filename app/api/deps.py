from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import User


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.headers.get("authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:]
    elif request.cookies.get("auth"):
        token = request.cookies.get("auth", "")
    if not token:
        raise HTTPException(401, "Not authenticated")
    import jwt as pyjwt

    from app.core.config import settings

    try:
        payload = pyjwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except pyjwt.PyJWTError:
        raise HTTPException(401, "Invalid token")
    user = db.get(User, payload.get("sub"))
    if not user:
        raise HTTPException(401, "User not found")
    return user
