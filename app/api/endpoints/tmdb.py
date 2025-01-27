from typing import List, Any

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.recommend import RecommendChain
from app.chain.tmdb import TmdbChain
from app.core.security import verify_token
from app.schemas.types import MediaType

router = APIRouter()


@router.get("/seasons/{tmdbid}", summary="TMDB所有季", response_model=List[schemas.TmdbSeason])
def tmdb_seasons(tmdbid: int, _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID查询themoviedb所有季信息
    """
    seasons_info = TmdbChain().tmdb_seasons(tmdbid=tmdbid)
    if seasons_info:
        return seasons_info
    return []


@router.get("/similar/{tmdbid}/{type_name}", summary="类似电影/电视剧", response_model=List[schemas.MediaInfo])
def tmdb_similar(tmdbid: int,
                 type_name: str,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID查询类似电影/电视剧，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        medias = TmdbChain().movie_similar(tmdbid=tmdbid)
    elif mediatype == MediaType.TV:
        medias = TmdbChain().tv_similar(tmdbid=tmdbid)
    else:
        return []
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/recommend/{tmdbid}/{type_name}", summary="推荐电影/电视剧", response_model=List[schemas.MediaInfo])
def tmdb_recommend(tmdbid: int,
                   type_name: str,
                   _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID查询推荐电影/电视剧，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        medias = TmdbChain().movie_recommend(tmdbid=tmdbid)
    elif mediatype == MediaType.TV:
        medias = TmdbChain().tv_recommend(tmdbid=tmdbid)
    else:
        return []
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/collection/{collection_id}", summary="系列合集详情", response_model=List[schemas.MediaInfo])
def tmdb_collection(collection_id: int,
                    page: int = 1,
                    count: int = 20,
                    _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据合集ID查询合集详情
    """
    medias = TmdbChain().tmdb_collection(collection_id=collection_id)
    if medias:
        return [media.to_dict() for media in medias][(page - 1) * count:page * count]
    return []


@router.get("/credits/{tmdbid}/{type_name}", summary="演员阵容", response_model=List[schemas.MediaPerson])
def tmdb_credits(tmdbid: int,
                 type_name: str,
                 page: int = 1,
                 _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID查询演员阵容，type_name: 电影/电视剧
    """
    mediatype = MediaType(type_name)
    if mediatype == MediaType.MOVIE:
        persons = TmdbChain().movie_credits(tmdbid=tmdbid, page=page)
    elif mediatype == MediaType.TV:
        persons = TmdbChain().tv_credits(tmdbid=tmdbid, page=page)
    else:
        return []
    return persons or []


@router.get("/person/{person_id}", summary="人物详情", response_model=schemas.MediaPerson)
def tmdb_person(person_id: int,
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据人物ID查询人物详情
    """
    return TmdbChain().person_detail(person_id=person_id)


@router.get("/person/credits/{person_id}", summary="人物参演作品", response_model=List[schemas.MediaInfo])
def tmdb_person_credits(person_id: int,
                        page: int = 1,
                        _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据人物ID查询人物参演作品
    """
    medias = TmdbChain().person_credits(person_id=person_id, page=page)
    if medias:
        return [media.to_dict() for media in medias]
    return []


@router.get("/movies", summary="TMDB电影", response_model=List[schemas.MediaInfo])
def tmdb_movies(sort_by: str = "popularity.desc",
                with_genres: str = "",
                with_original_language: str = "",
                with_keywords: str = "",
                with_watch_providers: str = "",
                vote_average: float = 0,
                vote_count: int = 0,
                release_date: str = "",
                page: int = 1,
                _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览TMDB电影信息
    """
    return RecommendChain().tmdb_movies(sort_by=sort_by,
                                        with_genres=with_genres,
                                        with_original_language=with_original_language,
                                        with_keywords=with_keywords,
                                        with_watch_providers=with_watch_providers,
                                        vote_average=vote_average,
                                        vote_count=vote_count,
                                        release_date=release_date,
                                        page=page)


@router.get("/tvs", summary="TMDB剧集", response_model=List[schemas.MediaInfo])
def tmdb_tvs(sort_by: str = "popularity.desc",
             with_genres: str = "",
             with_original_language: str = "",
             with_keywords: str = "",
             with_watch_providers: str = "",
             vote_average: float = 0,
             vote_count: int = 0,
             release_date: str = "",
             page: int = 1,
             _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    浏览TMDB剧集信息
    """
    return RecommendChain().tmdb_tvs(sort_by=sort_by,
                                     with_genres=with_genres,
                                     with_original_language=with_original_language,
                                     with_keywords=with_keywords,
                                     with_watch_providers=with_watch_providers,
                                     vote_average=vote_average,
                                     vote_count=vote_count,
                                     release_date=release_date,
                                     page=page)


@router.get("/trending", summary="TMDB流行趋势", response_model=List[schemas.MediaInfo])
def tmdb_trending(page: int = 1,
                  _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    TMDB流行趋势
    """
    return RecommendChain().tmdb_trending(page=page)


@router.get("/{tmdbid}/{season}", summary="TMDB季所有集", response_model=List[schemas.TmdbEpisode])
def tmdb_season_episodes(tmdbid: int, season: int,
                         _: schemas.TokenPayload = Depends(verify_token)) -> Any:
    """
    根据TMDBID查询某季的所有信信息
    """
    return TmdbChain().tmdb_episodes(tmdbid=tmdbid, season=season)
