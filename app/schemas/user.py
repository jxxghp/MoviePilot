from typing import Optional

from pydantic import BaseModel


# Shared properties
class UserBase(BaseModel):
    name: str
    email: Optional[str] = None
    is_active: Optional[bool] = True
    is_superuser: bool = False
    avatar: Optional[str] = None


# Properties to receive via API on creation
class UserCreate(UserBase):
    name: str
    email: Optional[str] = None
    password: str


# Properties to receive via API on update
class UserUpdate(UserBase):
    name: str
    password: Optional[str] = None


class UserInDBBase(UserBase):
    id: Optional[int] = None

    class Config:
        orm_mode = True


# Additional properties to return via API
class User(UserInDBBase):
    name: str
    email: Optional[str] = None


# Additional properties stored in DB
class UserInDB(UserInDBBase):
    hashed_password: str
