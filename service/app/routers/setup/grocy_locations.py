"""The buttons on the two Grocy location items in the inbox: merge the two
fridges, and mark the Freezer as a freezer (services/grocy_locations.py).

Under /setup, so only someone who may change settings can press them, and
both refuse on a Bandit: its Grocy is the main server's.
"""

from fastapi import Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ...database import get_db
from ...services import grocy_locations
from ._common import router


def _answer(result: dict):
    status = result.pop("status", 200)
    return result if result.get("ok") else JSONResponse(result, status_code=status)


@router.post("/grocy/merge-fridges")
async def grocy_merge_fridges(db: Session = Depends(get_db)):
    return _answer(await grocy_locations.merge_fridges(db))


@router.post("/grocy/mark-freezer")
async def grocy_mark_freezer(db: Session = Depends(get_db)):
    return _answer(await grocy_locations.mark_freezer(db))
