from typing import Optional

from pydantic import BaseModel


class DownloadHistory(BaseModel):
    id: int
    path: Optional[str] = None
    type: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    tmdbid: Optional[int] = None
    imdbid: Optional[str] = None
    tvdbid: Optional[int] = None
    doubanid: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    download_hash: Optional[str] = None
    torrent_name: Optional[str] = None
    torrent_description: Optional[str] = None
    torrent_site: Optional[str] = None
    note: Optional[str] = None

    class Config:
        orm_mode = True


class TransferHistory(BaseModel):
    id: int
    src: Optional[str] = None
    dest: Optional[str] = None
    mode: Optional[str] = None
    type: Optional[str] = None
    category: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    tmdbid: Optional[int] = None
    imdbid: Optional[str] = None
    tvdbid: Optional[int] = None
    doubanid: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    download_hash: Optional[str] = None
    date: Optional[str] = None

    class Config:
        orm_mode = True
