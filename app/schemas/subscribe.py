from typing import Optional

from pydantic import BaseModel


class Subscribe(BaseModel):
    id: int
    name: str
    year: str
    type: str
    keyword: Optional[str]
    tmdbid: str
    doubanid: Optional[str]
    season: Optional[int]
    image: Optional[str]
    description: Optional[str]
    filter: Optional[str]
    include: Optional[str]
    exclude: Optional[str]
    total_episode: Optional[int]
    start_episode: Optional[int]
    lack_episode: Optional[int]
    note: Optional[str]
    state: str

    class Config:
        orm_mode = True
