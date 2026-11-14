"""
豆瓣短评与影评的稳定结果模型。
"""

from typing import Optional

from pydantic import BaseModel, Field


class MediaCommentAuthor(BaseModel):
    """
    评论作者的身份投影。
    """

    id: Optional[str] = Field(default=None, description="作者 ID")
    name: Optional[str] = Field(default=None, description="作者昵称")
    avatar: Optional[str] = Field(default=None, description="作者头像地址")
    url: Optional[str] = Field(default=None, description="作者主页地址")


class MediaComment(BaseModel):
    """
    豆瓣短评条目，评分按豆瓣五分制归一。
    """

    id: Optional[str] = Field(default=None, description="短评 ID")
    comment: Optional[str] = Field(default=None, description="短评正文")
    rating: Optional[float] = Field(default=None, description="五星制评分")
    vote_count: Optional[int] = Field(default=None, description="有用数")
    create_time: Optional[str] = Field(default=None, description="发表时间")
    author: Optional[MediaCommentAuthor] = Field(default=None, description="作者信息")
    url: Optional[str] = Field(default=None, description="短评地址")


class MediaReview(BaseModel):
    """
    豆瓣影评条目，正文以摘要形式返回。
    """

    id: Optional[str] = Field(default=None, description="影评 ID")
    title: Optional[str] = Field(default=None, description="影评标题")
    abstract: Optional[str] = Field(default=None, description="影评摘要")
    rating: Optional[float] = Field(default=None, description="五星制评分")
    useful_count: Optional[int] = Field(default=None, description="有用数")
    comments_count: Optional[int] = Field(default=None, description="回应数")
    create_time: Optional[str] = Field(default=None, description="发表时间")
    author: Optional[MediaCommentAuthor] = Field(default=None, description="作者信息")
    url: Optional[str] = Field(default=None, description="影评地址")


class MediaCommentPage(BaseModel):
    """
    短评分页结果，total 为豆瓣侧的总条目数。
    """

    start: int = Field(default=0, description="起始偏移")
    count: int = Field(default=0, description="本页条目数")
    total: int = Field(default=0, description="总条目数")
    items: list[MediaComment] = Field(default_factory=list, description="短评列表")


class MediaReviewPage(BaseModel):
    """
    影评分页结果，total 为豆瓣侧的总条目数。
    """

    start: int = Field(default=0, description="起始偏移")
    count: int = Field(default=0, description="本页条目数")
    total: int = Field(default=0, description="总条目数")
    items: list[MediaReview] = Field(default_factory=list, description="影评列表")
