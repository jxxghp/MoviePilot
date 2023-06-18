from typing import Optional

from pydantic import BaseModel


class Response(BaseModel):
    success: bool
    message: Optional[str] = None
    data: Optional[dict] = {}
