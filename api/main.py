import json
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from api.auth import hash_password
from api.database import Base, SessionLocal, engine
from api.db_models import User
from api.rate_limit import limiter
from api.routers import agent, auth

load_dotenv()


def _get_seed_users() -> list[dict]:
	raw = os.getenv("SEED_USERS", "[]")
	try:
		users = json.loads(raw)
		if not isinstance(users, list):
			raise ValueError
		return users
	except (json.JSONDecodeError, ValueError):
		return []


@asynccontextmanager
async def lifespan(app: FastAPI):
	# Create tables
	Base.metadata.create_all(bind=engine)

	# Clear existing users and re-seed from env
	db = SessionLocal()
	try:
		db.query(User).delete()
		db.commit()

		for user_data in _get_seed_users():
			user = User(
				username=user_data["username"],
				hashed_password=hash_password(user_data["password"]),
			)
			db.add(user)
		db.commit()
	finally:
		db.close()

	yield


app = FastAPI(
	title="CodeGen Agent API",
	version="0.1.0",
	lifespan=lifespan,
)

# Rate limiter
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
	return JSONResponse(
		status_code=429,
		content={"detail": "Rate limit exceeded: 5 requests per minute"},
	)


# CORS for Svelte dev server
app.add_middleware(
	CORSMiddleware,
	allow_origins=["http://localhost:5173"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

# Routers
app.include_router(auth.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health_check():
	return {"status": "ok"}
