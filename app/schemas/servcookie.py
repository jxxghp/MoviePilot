from fastapi import Query
from pydantic import BaseModel


class CookieData(BaseModel):
    encrypted: str = Query(min_length=1, max_length=1024 * 1024 * 50)
    uuid: str = Query(min_length=5, pattern="^[a-zA-Z0-9]+$")


class CookiePassword(BaseModel):
    password: str
