from typing import Optional

from pydantic import BaseModel


class Site(BaseModel):
    id: int
    name: str
    domain: str
    url: str
    pri: Optional[int] = 0
    rss: Optional[str] = None
    cookie: Optional[str] = None
    ua: Optional[str] = None
    proxy: Optional[int] = 0
    filter: Optional[str] = None
    note: Optional[str] = None
    limit_interval: Optional[int] = 0
    limit_count: Optional[int] = 0
    limit_seconds: Optional[int] = 0
    is_active: Optional[str] = 'N'

    class Config:
        orm_mode = True
