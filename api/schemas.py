from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class LoginRequest(BaseModel):
	username: str
	password: str


class TokenResponse(BaseModel):
	access_token: str
	token_type: str = "bearer"


class JobStatus(str, Enum):
	PENDING = "pending"
	RUNNING = "running"
	COMPLETED = "completed"
	FAILED = "failed"


class RunResponse(BaseModel):
	job_id: str
	status: JobStatus


class JobStatusResponse(BaseModel):
	job_id: str
	status: JobStatus
	created_at: datetime
	result: dict | None = None
	error: str | None = None
	output_files: list[str] = []
