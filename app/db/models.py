import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def uuid_default() -> str:
    return uuid.uuid4().hex


class Organization(Base):
    __tablename__ = "organization"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_default)
    name: Mapped[str] = mapped_column(String(255), default="My organization")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    apiKey: Mapped[str] = mapped_column(String(64), default=uuid_default, unique=True)

    users: Mapped[list["User"]] = relationship(back_populates="organization")
    integrations: Mapped[list["Integration"]] = relationship(back_populates="organization")


class User(Base):
    __tablename__ = "user"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_default)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255), default="")
    organizationId: Mapped[str] = mapped_column(ForeignKey("organization.id"))
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    organization: Mapped[Organization] = relationship(back_populates="users")


class Integration(Base):
    __tablename__ = "integration"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_default)
    organizationId: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    providerIdentifier: Mapped[str] = mapped_column(String(64), index=True)
    internalId: Mapped[str] = mapped_column(String(255), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    picture: Mapped[str] = mapped_column(String(1024), default="")
    username: Mapped[str] = mapped_column(String(255), default="")
    token: Mapped[str] = mapped_column(Text, default="")
    refreshToken: Mapped[str] = mapped_column(Text, default="")
    tokenExpiration: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    additionalSettings: Mapped[str] = mapped_column(Text, default="{}")
    customInstanceDetails: Mapped[str] = mapped_column(Text, default="")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    refreshNeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    inBetweenSteps: Mapped[bool] = mapped_column(Boolean, default=False)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    organization: Mapped[Organization] = relationship(back_populates="integrations")
    posts: Mapped[list["Post"]] = relationship(back_populates="integration")


class Post(Base):
    __tablename__ = "post"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_default)
    organizationId: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    integrationId: Mapped[str] = mapped_column(ForeignKey("integration.id"), index=True)
    state: Mapped[str] = mapped_column(String(16), default="QUEUE", index=True)
    publishDate: Mapped[datetime] = mapped_column(DateTime, index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(Text, default="[]")
    settings: Mapped[str] = mapped_column(Text, default="{}")
    group: Mapped[str] = mapped_column(String(32), default=uuid_default, index=True)
    parentPostId: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    delay: Mapped[int] = mapped_column(Integer, default=0)
    releaseId: Mapped[str | None] = mapped_column(String(255), nullable=True)
    releaseURL: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pendingData: Mapped[str | None] = mapped_column(Text, nullable=True)
    pendingChecks: Mapped[int] = mapped_column(Integer, default=0)
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    integration: Mapped[Integration] = relationship(back_populates="posts")


class Media(Base):
    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_default)
    organizationId: Mapped[str] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(512))
    originalName: Mapped[str] = mapped_column(String(512), default="")
    path: Mapped[str] = mapped_column(String(1024))
    fileSize: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(16), default="image")
    alt: Mapped[str] = mapped_column(Text, default="")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ErrorRecord(Base):
    __tablename__ = "errors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uuid_default)
    organizationId: Mapped[str] = mapped_column(String(32), index=True)
    platform: Mapped[str] = mapped_column(String(64), default="")
    postId: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    createdAt: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
