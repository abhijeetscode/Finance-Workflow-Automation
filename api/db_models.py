from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from api.database import Base


class User(Base):
	__tablename__ = "users"

	id: Mapped[int] = mapped_column(primary_key=True)
	username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
	hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)


class Job(Base):
	__tablename__ = "jobs"

	job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
	username: Mapped[str] = mapped_column(String(50), nullable=False)
	status: Mapped[str] = mapped_column(String(20), default="pending")
	created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
	result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
	error: Mapped[str | None] = mapped_column(String, nullable=True)
	output_files: Mapped[list | None] = mapped_column(JSON, nullable=True)
