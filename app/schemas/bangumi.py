from typing import Optional

from pydantic import BaseModel


class BangumiPerson(BaseModel):
    id: Optional[int] = None
    name: Optional[str] = None
    type: Optional[int] = 1
    career: Optional[list] = []
    images: Optional[dict] = {}
    relation: Optional[str] = None
