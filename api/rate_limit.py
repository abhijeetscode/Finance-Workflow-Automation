import jwt
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.auth import JWT_ALGORITHM, JWT_SECRET


def _get_username_from_token(request: Request) -> str:
	"""Extract username from JWT bearer token for rate limiting key."""
	auth = request.headers.get("authorization", "")
	if auth.startswith("Bearer "):
		token = auth[7:]
		try:
			payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
			username = payload.get("sub")
			if username:
				return username
		except jwt.InvalidTokenError:
			pass
	return get_remote_address(request)


limiter = Limiter(key_func=_get_username_from_token)
