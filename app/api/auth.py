from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.security import create_token, hash_password, verify_password
from app.db.database import get_db
from app.db.models import Organization, User
from app.api.deps import get_current_user

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterBody(BaseModel):
    email: EmailStr
    password: str
    name: str = ""
    organization: str = "My organization"


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    organizationId: str


def _user_out(user: User) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "organizationId": user.organizationId}


@router.post("/register")
def register(body: RegisterBody, db: Session = Depends(get_db)):
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(409, "Email already registered")
    org = Organization(name=body.organization)
    db.add(org)
    db.flush()
    user = User(email=body.email, password=hash_password(body.password), name=body.name, organizationId=org.id)
    db.add(user)
    db.commit()
    return {"token": create_token(user.id, org.id), "user": _user_out(user)}


@router.post("/login")
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.password):
        raise HTTPException(401, "Invalid credentials")
    return {"token": create_token(user.id, user.organizationId), "user": _user_out(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return _user_out(user)
