"""A minimal endpoint whose only job is answering "is this session actually
authorized" - no business logic, no extra database queries beyond what
get_authenticated_user_id already does. The frontend uses this to decide
whether to render the app shell at all, rather than briefly showing it and
reacting to a 401 from a real data endpoint after the fact.
"""

from fastapi import APIRouter, Depends

from app.dependencies.auth import get_authenticated_user_id

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/whoami")
def whoami(user_id: str = Depends(get_authenticated_user_id)) -> dict:
    return {"userId": user_id}
