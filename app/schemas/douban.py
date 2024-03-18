from typing import Optional

from pydantic import BaseModel


class DoubanPerson(BaseModel):
    id: Optional[str] = None
    name: Optional[str] = None
    roles: Optional[list] = []
    title: Optional[str] = None
    url: Optional[str] = None
    character: Optional[str] = None
    avatar: Optional[dict] = None
    latin_name: Optional[str] = None
