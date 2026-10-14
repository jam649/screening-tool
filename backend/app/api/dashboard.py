from fastapi import APIRouter

from app.api.deps import DB, CurrentUser
from app.reports.metrics import dashboard_metrics

router = APIRouter()


@router.get("/dashboard")
def dashboard(user: CurrentUser, db: DB):
    return dashboard_metrics(db)
