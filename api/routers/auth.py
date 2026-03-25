from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from api.auth import create_access_token, verify_password
from api.database import get_db
from api.db_models import User
from api.schemas import LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
	user = db.query(User).filter(User.username == body.username).first()
	if not user or not verify_password(body.password, user.hashed_password):
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Invalid username or password",
		)
	token = create_access_token(user.username)
	return TokenResponse(access_token=token)
