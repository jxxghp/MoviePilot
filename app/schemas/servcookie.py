from typing import Union

from fastapi import Query
from pydantic import BaseModel


class CookieData(BaseModel):
    uuid: str = Query(min_length=5, pattern="^[a-zA-Z0-9]+$")
    encrypted: str = Query(min_length=1, max_length=1024 * 1024 * 50)


class CookiePassword(BaseModel):
    password: str
