import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.getenv("JWT_SECRET", "zalos-take-home-secret-key-min-32bytes!")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 30

security = HTTPBearer()


def hash_password(password: str) -> str:
	return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
	return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(username: str) -> str:
	payload = {
		"sub": username,
		"exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRY_MINUTES),
	}
	return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> str:
	"""Decode JWT and return username. Raises HTTPException on failure."""
	try:
		payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
		username: str | None = payload.get("sub")
		if username is None:
			raise HTTPException(
				status_code=status.HTTP_401_UNAUTHORIZED,
				detail="Invalid token: missing subject",
			)
		return username
	except jwt.ExpiredSignatureError:
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Token has expired",
		)
	except jwt.InvalidTokenError:
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Invalid token",
		)


def get_current_user(
	credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
	"""FastAPI dependency for REST endpoints — extracts username from bearer token."""
	return _decode_token(credentials.credentials)


def get_ws_user(token: str) -> str:
	"""For WebSocket auth — token passed as query param."""
	return _decode_token(token)
