import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse

from app.agent.skills.metadata import parse_skill_metadata
from app.runtime.cache import cached, fresh
from app.runtime.settings import RuntimeSettingsCompat

settings = RuntimeSettingsCompat()
from app.runtime.log import logger
from app.adapters.network.http import RequestUtils
from app.foundation.singleton import WeakSingleton
from app.foundation.url import UrlUtils

_SOURCE_META_FILENAME = ".moviepilot-skill-source.json"
_DEFAULT_BRANCHES = ("main", "master")
_MARKET_CACHE_TTL = 60 * 30
_CLAWHUB_HOSTS = {"clawhub.ai", "www.clawhub.ai"}
_CLAWHUB_CONVEX_CLIENT = "npm-1.35.1"
_CLAWHUB_LIST_QUERY = "skills:listPublicPageV4"
_CLAWHUB_LIST_PAGE_SIZE = 100
_CLAWHUB_LIST_MAX_PAGES = 2
_OFFICIAL_SKILL_REPOS = {
    "openai/skills",
    "anthropics/skills",
    "vercel-labs/agent-skills",
    "NousResearch/hermes-agent",
}


@dataclass
class SkillInfo:
    """描述本地或市场中的一个 Skill。"""

    id: str
    name: str
    description: str
    version: int = 0
    path: str = ""
    source_type: str = "local"
    source_label: str = "本地"
    repo_url: Optional[str] = None
    repo_name: Optional[str] = None
    skill_path: Optional[str] = None
    registry_url: Optional[str] = None
    registry_name: Optional[str] = None
    registry_slug: Optional[str] = None
    download_url: Optional[str] = None
    installed: bool = False
    removable: bool = False


@dataclass
class SkillMarketSource:
    """描述一个 Skill 市场来源及其可移除状态。"""

    source: str
    label: str
    builtin: bool = True
    removable: bool = False


class SkillHelper(metaclass=WeakSingleton):
    """
    技能市场与本地技能管理
    """

    @staticmethod
    def get_user_skills_dir() -> Path:
        """
        返回用户技能目录，所有市场安装的技能都落在这里。
        """
        return settings.CONFIG_PATH / "agent" / "skills"

    @staticmethod
    def get_bundled_skills_dir() -> Path:
        """
        返回仓库内置技能目录。
        """
        return settings.ROOT_PATH / "skills"

    @staticmethod
    def get_market_sources() -> List[str]:
        """
        解析配置中的技能市场列表。
        """
        if not settings.SKILL_MARKET:
            return []
        return [item.strip() for item in settings.SKILL_MARKET.split(",") if item.strip()]

    @staticmethod
    def get_default_market_sources() -> List[str]:
        """
        返回系统默认的技能市场列表，用于区分内置源和用户追加源。
        """
        skill_market_field = type(settings).model_fields.get("SKILL_MARKET")
        default_value = skill_market_field.default if skill_market_field else None
        if not default_value:
            return []
        return [item.strip() for item in str(default_value).split(",") if item.strip()]

    @staticmethod
    def _ensure_user_skills_dir() -> Path:
        """
        确保用户技能目录存在，供安装和扫描复用。
        """
        skill_dir = SkillHelper.get_user_skills_dir()
        skill_dir.mkdir(parents=True, exist_ok=True)
        return skill_dir

    @staticmethod
    def _canonical_market_source(source: str) -> Optional[str]:
        """
        生成市场源的规范化值，用于去重、默认源比对和持久化。
        """
        normalized = (source or "").strip()
        if not normalized:
            return None

        registry = SkillHelper._parse_market_registry(normalized)
        if registry:
            return registry["registry_url"].rstrip("/")

        repo = SkillHelper._parse_market_repo(normalized)
        if repo:
            # 对 GitHub 仓库保留分支和技能根目录，避免不同路径的技能仓库混淆。
            if repo["branch"]:
                return (
                    f"{repo['repo_url']}/tree/"
                    f"{repo['branch']}/{repo['root_path'].strip('/')}"
                ).rstrip("/")
            return repo["repo_url"].rstrip("/")

        return normalized.rstrip("/")

    @staticmethod
    def _build_repo_source_label(repo_name: Optional[str]) -> str:
        """
        根据仓库名称生成展示标签。
        """
        repo_name = (repo_name or "").strip()
        if not repo_name:
            return "仓库来源"
        if repo_name in _OFFICIAL_SKILL_REPOS:
            return f"官方仓库 · {repo_name}"
        return f"仓库来源 · {repo_name}"

    @staticmethod
    def _build_registry_source_label(registry_name: Optional[str]) -> str:
        """
        根据注册表名称生成展示标签。
        """
        registry_name = (registry_name or "").strip()
        if not registry_name:
            return "社区注册表"
        return f"社区注册表 · {registry_name}"

    def describe_market_source(self, source: str) -> str:
        """
        将配置中的市场源地址转换为更适合用户阅读的描述。
        """
        registry = self._parse_market_registry(source)
        if registry:
            return self._build_registry_source_label(registry.get("registry_name"))

        repo = self._parse_market_repo(source)
        if repo:
            return self._build_repo_source_label(repo.get("repo_name"))
        return source

    def list_market_source_entries(self) -> List[SkillMarketSource]:
        """
        返回当前技能源及其是否属于内置默认源的展示信息。
        """
        default_keys = {
            self._canonical_market_source(item) for item in self.get_default_market_sources()
        }
        results: List[SkillMarketSource] = []
        for source in self.get_market_sources():
            source_key = self._canonical_market_source(source)
            builtin = source_key in default_keys
            results.append(
                SkillMarketSource(
                    source=source,
                    label=self.describe_market_source(source),
                    builtin=builtin,
                    removable=not builtin,
                )
            )
        return results

    @staticmethod
    def _persist_market_sources(sources: List[str]) -> Tuple[bool, str]:
        """
        将技能源列表写回配置文件，并同步更新内存中的 settings。
        """
        filtered_sources = [item.strip() for item in sources if item and item.strip()]
        success, message = settings.update_setting(
            key="SKILL_MARKET",
            value=",".join(filtered_sources),
        )
        if success is False:
            return False, message
        return True, message

    def add_custom_market_source(self, source: str) -> Tuple[bool, str]:
        """
        添加自定义 GitHub 技能源，支持 owner/repo 与 GitHub URL 两种写法。
        """
        repo = self._parse_market_repo(source)
        if not repo:
            return (
                False,
                "仅支持 GitHub skills 仓库，示例：openai/skills 或 https://github.com/openai/skills",
            )

        canonical_source = self._canonical_market_source(source)
        if not canonical_source:
            return False, "技能源地址不能为空"

        existing_keys = {
            self._canonical_market_source(item) for item in self.get_market_sources()
        }
        if canonical_source in existing_keys:
            return False, "该技能源已存在"

        current_sources = self.get_market_sources()
        success, message = self._persist_market_sources(
            current_sources + [canonical_source]
        )
        if not success:
            return False, message
        return True, f"已添加技能源：{self.describe_market_source(canonical_source)}"

    def remove_custom_market_source(self, source: str) -> Tuple[bool, str]:
        """
        删除一个自定义技能源，内置默认源不允许移除。
        """
        canonical_source = self._canonical_market_source(source)
        if not canonical_source:
            return False, "技能源地址无效"

        default_keys = {
            self._canonical_market_source(item) for item in self.get_default_market_sources()
        }
        if canonical_source in default_keys:
            return False, f"技能源 {self.describe_market_source(source)} 是内置默认源，不能删除"

        current_sources = self.get_market_sources()
        remaining_sources = [
            item
            for item in current_sources
            if self._canonical_market_source(item) != canonical_source
        ]
        if len(remaining_sources) == len(current_sources):
            return False, "技能源不存在"

        success, message = self._persist_market_sources(remaining_sources)
        if not success:
            return False, message
        return True, f"已删除技能源：{self.describe_market_source(source)}"

    @staticmethod
    def _normalize_repo_url(repo_url: str) -> Optional[str]:
        """
        将技能市场配置统一归一为 GitHub HTTPS 地址。
        """
        repo_url = (repo_url or "").strip()
        if not repo_url:
            return None
        if repo_url.startswith(("http://", "https://")):
            return repo_url.rstrip("/")
        return f"https://github.com/{repo_url.strip('/')}"

    @staticmethod
    def _parse_market_repo(repo_url: str) -> Optional[dict]:
        """
        解析市场仓库地址，提取仓库、分支和技能根目录信息。
        """
        normalized = SkillHelper._normalize_repo_url(repo_url)
        if not normalized:
            return None
        parsed = urlparse(normalized)
        if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
            logger.warning("暂不支持的技能市场地址：%s", repo_url)
            return None

        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < 2:
            return None

        owner = parts[0]
        repo = parts[1].removesuffix(".git")
        branch = None
        root_path = "skills"
        if len(parts) >= 4 and parts[2] == "tree":
            branch = parts[3]
            if len(parts) > 4:
                root_path = "/".join(parts[4:])

        return {
            "repo_url": f"https://github.com/{owner}/{repo}",
            "repo_name": f"{owner}/{repo}",
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "root_path": root_path.strip("/") or "skills",
        }

    @staticmethod
    def _parse_market_registry(source_url: str) -> Optional[dict]:
        """
        解析注册表市场地址，目前支持 ClawHub。
        """
        normalized = (source_url or "").strip()
        if not normalized.startswith(("http://", "https://")):
            return None

        parsed = urlparse(normalized)
        if parsed.netloc.lower() not in _CLAWHUB_HOSTS:
            return None

        base_url = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        path = parsed.path.rstrip("/")
        api_base = (
            f"{base_url}{path}"
            if path.endswith("/api/v1")
            else f"{base_url}/api/v1"
        )
        return {
            "registry_url": base_url,
            "registry_name": "ClawHub",
            "api_base": api_base.rstrip("/"),
        }

    @staticmethod
    def _read_source_meta(skill_dir: Path) -> dict:
        """
        读取技能来源元数据，用于区分本地、市场和内置技能。
        """
        meta_path = skill_dir / _SOURCE_META_FILENAME
        if not meta_path.exists():
            return {}
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
            return payload if isinstance(payload, dict) else {}
        except Exception as e:
            logger.warning("读取技能来源元数据失败：%s - %s", meta_path, e)
            return {}

    @staticmethod
    def _write_source_meta(skill_dir: Path, payload: dict) -> None:
        """
        写入技能来源元数据，便于后续展示来源和追踪安装来源。
        """
        meta_path = skill_dir / _SOURCE_META_FILENAME
        meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _is_bundled_skill(skill_id: str) -> bool:
        """
        判断技能是否来自仓库内置目录。
        """
        return (SkillHelper.get_bundled_skills_dir() / skill_id / "SKILL.md").exists()

    def list_local_skills(self) -> List[SkillInfo]:
        """
        扫描本地已安装技能，并补充来源和是否可删除等展示信息。
        """
        skill_root = self._ensure_user_skills_dir()
        results: List[SkillInfo] = []

        for path in sorted(skill_root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir():
                continue
            skill_md = path / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning("读取技能文件失败：%s - %s", skill_md, e)
                continue

            metadata = parse_skill_metadata(content, str(skill_md), path.name)
            if not metadata:
                continue

            bundled = self._is_bundled_skill(path.name)
            source_meta = self._read_source_meta(path)
            source_type = "bundled" if bundled else source_meta.get("source", "local")
            if source_type == "market":
                source_label = self._build_repo_source_label(
                    source_meta.get("repo_name") or source_meta.get("repo_url")
                )
            elif source_type == "registry":
                source_label = self._build_registry_source_label(
                    source_meta.get("registry_name") or source_meta.get("registry_url")
                )
            elif source_type == "bundled":
                source_label = "内置"
            else:
                source_label = "本地"

            results.append(
                SkillInfo(
                    id=path.name,
                    name=metadata["name"],
                    description=metadata["description"],
                    version=metadata["version"],
                    path=str(skill_md),
                    source_type=source_type,
                    source_label=source_label,
                    repo_url=source_meta.get("repo_url"),
                    repo_name=source_meta.get("repo_name"),
                    skill_path=source_meta.get("skill_path"),
                    registry_url=source_meta.get("registry_url"),
                    registry_name=source_meta.get("registry_name"),
                    registry_slug=source_meta.get("registry_slug"),
                    download_url=source_meta.get("download_url"),
                    installed=True,
                    removable=not bundled,
                )
            )

        return results

    def list_market_skills(self, force: bool = False) -> List[SkillInfo]:
        """
        聚合所有市场源的技能，并用本地技能状态标记已安装项。
        """
        local_skills = self.list_local_skills()
        local_ids = {skill.id for skill in local_skills}
        local_names = {skill.name for skill in local_skills}

        deduped: Dict[str, SkillInfo] = {}
        for source in self.get_market_sources():
            with fresh(force):
                market_skills = self._list_market_source_skills(source)
            for skill in market_skills:
                key = skill.name or skill.id
                if key in deduped:
                    continue
                skill.installed = skill.id in local_ids or skill.name in local_names
                deduped[key] = skill

        return sorted(
            deduped.values(),
            key=lambda item: (
                item.installed,
                (item.repo_name or item.registry_name or "").lower(),
                item.name.lower(),
            ),
        )

    @staticmethod
    def filter_market_skills(
        skills: List[SkillInfo],
        query: str,
    ) -> List[SkillInfo]:
        """
        按关键词过滤市场技能。

        搜索范围覆盖技能 ID、名称、描述以及来源标签；多词查询按 AND 语义匹配，
        便于用户逐步缩小候选范围。
        """
        normalized_query = (query or "").strip().lower()
        if not normalized_query:
            return skills

        terms = [term for term in re.split(r"\s+", normalized_query) if term]
        if not terms:
            return skills

        results: List[SkillInfo] = []
        for skill in skills:
            haystack = " ".join(
                filter(
                    None,
                    [
                        skill.id,
                        skill.name,
                        skill.description,
                        skill.source_label,
                        skill.repo_name,
                        skill.registry_name,
                    ],
                )
            ).lower()
            if all(term in haystack for term in terms):
                results.append(skill)
        return results

    @cached(maxsize=24, ttl=_MARKET_CACHE_TTL, skip_empty=True)
    def _list_market_source_skills(self, source: str) -> List[SkillInfo]:
        """
        根据市场源类型分发到仓库扫描或注册表读取。
        """
        registry = self._parse_market_registry(source)
        if registry:
            return self._list_market_registry_skills(registry)
        return self._list_market_repo_skills(source)

    @cached(maxsize=16, ttl=_MARKET_CACHE_TTL, skip_empty=True)
    def _list_market_repo_skills(self, repo_url: str) -> List[SkillInfo]:
        """
        读取单个市场仓库中的技能列表。

        仓库按 zip 方式拉取后直接在压缩包内解析，避免落地整个仓库。
        """
        repo = self._parse_market_repo(repo_url)
        if not repo:
            return []

        repo_bytes = self._download_repo_archive(repo)
        if not repo_bytes:
            return []

        try:
            with zipfile.ZipFile(io.BytesIO(repo_bytes)) as zf:
                names = zf.namelist()
                if not names:
                    return []
                root_prefix = names[0].split("/", 1)[0] + "/"
                results: List[SkillInfo] = []
                seen_paths = set()
                for archive_name in names:
                    if not archive_name.endswith("/SKILL.md"):
                        continue
                    if not archive_name.startswith(root_prefix):
                        continue

                    rel_path = archive_name[len(root_prefix):].strip("/")
                    if not rel_path.startswith(f"{repo['root_path'].strip('/')}/"):
                        continue
                    if "/.system/" in f"/{rel_path}/":
                        continue
                    if rel_path in seen_paths:
                        continue
                    seen_paths.add(rel_path)

                    skill_dir = rel_path[: -len("/SKILL.md")]
                    skill_id = Path(skill_dir).name
                    try:
                        content = zf.read(archive_name).decode("utf-8")
                    except Exception as e:
                        logger.warning("读取市场技能失败：%s - %s", archive_name, e)
                        continue

                    metadata = parse_skill_metadata(
                        content,
                        f"{repo['repo_url']}:{rel_path}",
                        skill_id,
                    )
                    if not metadata:
                        continue

                    results.append(
                        SkillInfo(
                            id=skill_id,
                            name=metadata["name"],
                            description=metadata["description"],
                            version=metadata["version"],
                            path=f"{repo['repo_url']}/tree/{repo['branch']}/{skill_dir}",
                            source_type="market",
                            source_label=self._build_repo_source_label(
                                repo["repo_name"]
                            ),
                            repo_url=repo["repo_url"],
                            repo_name=repo["repo_name"],
                            skill_path=skill_dir,
                            installed=False,
                            removable=False,
                        )
                    )
                return results
        except Exception as e:
            logger.error("解析技能市场压缩包失败：%s", e)
            return []

    def _list_market_registry_skills(self, registry: dict) -> List[SkillInfo]:
        """
        从注册表拉取技能列表。

        ClawHub 官方前端当前通过 Convex query 拉取公开技能列表，因此这里优先复用
        同一条查询链路；若运行时信息解析失败，再回退到旧的 REST 风格接口。
        """
        parsed = urlparse((registry.get("registry_url") or "").rstrip("/"))
        if parsed.netloc.lower() in _CLAWHUB_HOSTS:
            skills = self._list_clawhub_registry_skills(registry)
            if skills:
                return skills

        response = self._request_registry(
            url=f"{registry['api_base']}/skills",
            params={"limit": 200, "sort": "installsAllTime"},
        )
        if not response:
            return []

        payload = self._load_json_response(response)
        items = self._extract_registry_items(payload)
        results: List[SkillInfo] = []
        for item in items:
            skill = self._build_registry_skill(item, registry)
            if skill:
                results.append(skill)
        return results

    def install_market_skill(self, skill: SkillInfo) -> Tuple[bool, str]:
        """
        将市场技能安装到用户技能目录，并记录来源元数据。
        """
        target_root = self._ensure_user_skills_dir()
        target_dir = target_root / skill.id
        if target_dir.exists():
            return False, f"技能 {skill.id} 已存在"
        if self._is_bundled_skill(skill.id):
            return False, f"技能 {skill.id} 是 MoviePilot 内置技能，不能覆盖安装"

        if skill.registry_url:
            return self._install_registry_skill(skill, target_dir)

        if not skill.repo_url or not skill.skill_path:
            return False, "技能来源信息不完整，无法安装"

        repo = self._parse_market_repo(skill.repo_url)
        if not repo:
            return False, "技能市场地址无效"

        repo_bytes = self._download_repo_archive(repo)
        if not repo_bytes:
            return False, "下载技能仓库失败，请检查网络连接或 GitHub 配置"

        try:
            with zipfile.ZipFile(io.BytesIO(repo_bytes)) as zf:
                names = zf.namelist()
                if not names:
                    return False, "技能仓库为空"
                root_prefix = names[0].split("/", 1)[0] + "/"
                skill_prefix = f"{root_prefix}{skill.skill_path.strip('/')}/"
                matched = [name for name in names if name.startswith(skill_prefix)]
                if not matched:
                    return False, f"未找到技能目录：{skill.skill_path}"

                target_dir.mkdir(parents=True, exist_ok=False)
                try:
                    wrote = False
                    for archive_name in matched:
                        rel_name = archive_name[len(skill_prefix):]
                        if not rel_name:
                            continue
                        output_path = target_dir / rel_name
                        if archive_name.endswith("/"):
                            output_path.mkdir(parents=True, exist_ok=True)
                            continue
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(archive_name, "r") as src, open(output_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        wrote = True

                    if not wrote or not (target_dir / "SKILL.md").exists():
                        shutil.rmtree(target_dir, ignore_errors=True)
                        return False, "技能目录内容不完整，安装失败"

                    self._write_source_meta(
                        target_dir,
                        {
                            "source": "market",
                            "repo_url": repo["repo_url"],
                            "repo_name": repo["repo_name"],
                            "branch": repo["branch"],
                            "skill_path": skill.skill_path,
                            "installed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    return True, f"技能 {skill.id} 已安装到 {target_dir}"
                except Exception:
                    shutil.rmtree(target_dir, ignore_errors=True)
                    raise
        except Exception as e:
            logger.error("安装市场技能失败：%s", e)
            return False, f"安装技能失败：{e}"

    def _install_registry_skill(
        self, skill: SkillInfo, target_dir: Path
    ) -> Tuple[bool, str]:
        """
        从注册表下载并安装技能包。
        """
        if not skill.registry_url or not (skill.registry_slug or skill.id):
            return False, "注册表技能来源信息不完整，无法安装"

        archive_bytes = self._download_registry_archive(skill)
        if not archive_bytes:
            return False, "下载注册表技能失败，请检查网络连接"

        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
                if not zf.namelist():
                    return False, "注册表技能压缩包为空"

                target_dir.mkdir(parents=True, exist_ok=False)
                try:
                    wrote = self._extract_skill_archive(zf, target_dir)
                    if not wrote or not (target_dir / "SKILL.md").exists():
                        shutil.rmtree(target_dir, ignore_errors=True)
                        return False, "注册表技能内容不完整，安装失败"

                    self._write_source_meta(
                        target_dir,
                        {
                            "source": "registry",
                            "registry_url": skill.registry_url,
                            "registry_name": skill.registry_name,
                            "registry_slug": skill.registry_slug or skill.id,
                            "download_url": skill.download_url,
                            "installed_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                    return True, f"技能 {skill.id} 已安装到 {target_dir}"
                except Exception:
                    shutil.rmtree(target_dir, ignore_errors=True)
                    raise
        except Exception as e:
            logger.error("安装注册表技能失败：%s", e)
            return False, f"安装技能失败：{e}"

    def remove_local_skill(self, skill_id: str) -> Tuple[bool, str]:
        """
        删除一个本地技能目录，内置技能会被显式拦截。
        """
        if not skill_id:
            return False, "技能ID不能为空"
        if self._is_bundled_skill(skill_id):
            return False, f"技能 {skill_id} 是 MoviePilot 内置技能，不能删除"

        skill_dir = self._ensure_user_skills_dir() / skill_id
        if not skill_dir.exists():
            return False, f"技能 {skill_id} 不存在"
        if not (skill_dir / "SKILL.md").exists():
            return False, f"{skill_id} 不是有效的技能目录"

        try:
            shutil.rmtree(skill_dir)
            return True, f"技能 {skill_id} 已删除"
        except Exception as e:
            logger.error("删除技能失败：%s", e)
            return False, f"删除技能失败：{e}"

    @staticmethod
    def _load_json_response(response) -> dict:
        """
        读取 HTTP 响应中的 JSON 数据，异常时回退为空对象。
        """
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {"items": payload}
        except Exception as e:
            logger.warning("解析技能市场 JSON 响应失败：%s", e)
            return {}

    @staticmethod
    def _extract_registry_items(payload: dict) -> List[dict]:
        """
        从不同响应结构中提取技能列表。
        """
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        for key in ("items", "skills", "results"):
            items = payload.get(key)
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]

        for key in ("data", "result"):
            nested = payload.get(key)
            if not isinstance(nested, dict):
                continue
            for list_key in ("items", "skills", "results"):
                items = nested.get(list_key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    def _list_clawhub_registry_skills(self, registry: dict) -> List[SkillInfo]:
        """
        按 ClawHub 官方前端的调用方式，通过 Convex query 获取公开技能列表。
        """
        runtime_env = self._discover_clawhub_runtime_env(registry["registry_url"])
        convex_url = (runtime_env or {}).get("convex_url")
        if not convex_url:
            return []

        results: Dict[str, SkillInfo] = {}
        cursor = None
        for _ in range(_CLAWHUB_LIST_MAX_PAGES):
            args = {
                "numItems": _CLAWHUB_LIST_PAGE_SIZE,
                "sort": "downloads",
                "dir": "desc",
                "nonSuspiciousOnly": True,
            }
            if cursor:
                args["cursor"] = cursor

            response = self._request_convex_query(
                deployment_url=convex_url,
                path=_CLAWHUB_LIST_QUERY,
                args=args,
            )
            if not response:
                break

            payload = self._load_json_response(response)
            value = payload.get("value")
            if not isinstance(value, dict):
                break

            page = value.get("page")
            if not isinstance(page, list):
                break

            for item in page:
                if not isinstance(item, dict):
                    continue
                skill = self._build_registry_skill(item, registry)
                if skill and skill.id not in results:
                    results[skill.id] = skill

            if not value.get("hasMore") or not value.get("nextCursor"):
                break
            cursor = value["nextCursor"]

        return list(results.values())

    def _build_registry_skill(
        self, item: dict, registry: dict
    ) -> Optional[SkillInfo]:
        """
        将注册表返回的条目转换为统一的 SkillInfo。
        """
        skill_data = item.get("skill") if isinstance(item.get("skill"), dict) else item
        slug = (
            skill_data.get("slug")
            or item.get("slug")
            or skill_data.get("id")
            or item.get("id")
            or skill_data.get("name")
            or skill_data.get("displayName")
            or skill_data.get("title")
        )
        if not slug:
            return None

        name = (
            skill_data.get("name")
            or skill_data.get("displayName")
            or item.get("displayName")
            or skill_data.get("title")
            or item.get("title")
            or slug
        )
        description = (
            skill_data.get("description")
            or skill_data.get("summary")
            or item.get("description")
            or item.get("summary")
            or skill_data.get("excerpt")
            or item.get("excerpt")
            or ""
        )
        owner_handle = item.get("ownerHandle") or self._extract_registry_owner_handle(item)
        page_path = f"/{owner_handle}/{slug}" if owner_handle else f"/skills/{slug}"

        return SkillInfo(
            id=str(slug),
            name=str(name),
            description=str(description),
            version=0,
            path=f"{registry['registry_url']}{page_path}",
            source_type="registry",
            source_label=self._build_registry_source_label(
                registry["registry_name"]
            ),
            registry_url=registry["registry_url"],
            registry_name=registry["registry_name"],
            registry_slug=str(slug),
            download_url=skill_data.get("downloadUrl")
            or item.get("downloadUrl")
            or self._build_registry_download_url(registry["api_base"], str(slug)),
            installed=False,
            removable=False,
        )

    @staticmethod
    def _extract_registry_owner_handle(item: dict) -> Optional[str]:
        """
        尽量从注册表条目中提取作者/拥有者 handle。
        """
        for key in ("ownerHandle", "authorHandle", "handle", "username"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lstrip("@")

        for key in ("owner", "author", "user", "publisher"):
            nested = item.get(key)
            if not isinstance(nested, dict):
                continue
            for nested_key in ("handle", "username", "login", "name"):
                value = nested.get(nested_key)
                if isinstance(value, str) and value.strip():
                    return value.strip().lstrip("@")
        return None

    @staticmethod
    def _build_registry_download_url(api_base: str, slug: str) -> str:
        """
        根据官方文档约定构造注册表 ZIP 下载地址。
        """
        query = urlencode({"slug": slug})
        return f"{api_base.rstrip('/')}/download?{query}"

    def _download_registry_archive(self, skill: SkillInfo) -> Optional[bytes]:
        """
        下载注册表技能包 ZIP。
        """
        download_url = skill.download_url or self._build_registry_download_url(
            f"{skill.registry_url.rstrip('/')}/api/v1",
            skill.registry_slug or skill.id,
        )
        response = self._request_registry(url=download_url)
        if response is None or response.status_code != 200:
            logger.warning("下载注册表技能失败：%s", download_url)
            return None
        return response.content

    @cached(maxsize=4, ttl=_MARKET_CACHE_TTL, skip_empty=True)
    def _discover_clawhub_runtime_env(self, registry_url: str) -> Optional[dict]:
        """
        从 ClawHub 首页的 runtime env 脚本中提取当前生效的 Convex 部署地址。
        """
        response = self._request_registry(url=registry_url)
        if response is None or response.status_code != 200:
            return None

        html = self._read_response_text(response)
        runtime_asset_path = self._extract_runtime_env_asset_path(html)
        if not runtime_asset_path:
            return None

        runtime_asset_url = urljoin(f"{registry_url.rstrip('/')}/", runtime_asset_path)
        asset_response = self._request_registry(url=runtime_asset_url)
        if asset_response is None or asset_response.status_code != 200:
            return None

        script = self._read_response_text(asset_response)
        convex_url = self._extract_runtime_env_value(script, "VITE_CONVEX_URL")
        convex_site_url = self._extract_runtime_env_value(script, "VITE_CONVEX_SITE_URL")
        if not convex_url and not convex_site_url:
            return None
        return {
            "convex_url": convex_url.rstrip("/") if convex_url else None,
            "convex_site_url": convex_site_url.rstrip("/") if convex_site_url else None,
        }

    @staticmethod
    def _read_response_text(response) -> str:
        """
        尽量稳定地把 requests 响应或测试桩响应转换成文本。
        """
        text = getattr(response, "text", None)
        if isinstance(text, str):
            return text

        content = getattr(response, "content", b"")
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        if isinstance(content, str):
            return content
        return ""

    @staticmethod
    def _extract_runtime_env_asset_path(html: str) -> Optional[str]:
        """
        从 ClawHub 首页 HTML 中定位 runtime env 资源路径。
        """
        match = re.search(r'"/assets/runtimeEnv-[^"]+\.js"', html or "")
        if not match:
            return None
        return match.group(0).strip('"')

    @staticmethod
    def _extract_runtime_env_value(script: str, key: str) -> Optional[str]:
        """
        从运行时脚本中提取指定环境变量的值。
        """
        match = re.search(
            rf"{re.escape(key)}:\s*['\"`]([^'\"`]+)['\"`]",
            script or "",
        )
        if not match:
            return None
        return match.group(1).strip()

    @staticmethod
    def _extract_skill_archive(zf: zipfile.ZipFile, target_dir: Path) -> bool:
        """
        从技能压缩包中提取单个技能目录。

        兼容 `package/`、`skill-name/` 或直接根目录三种常见打包形式。
        """
        names = zf.namelist()
        skill_md_names = [
            name
            for name in names
            if name.endswith("SKILL.md") and "/.system/" not in f"/{name}/"
        ]
        if not skill_md_names:
            return False

        skill_md_name = min(skill_md_names, key=lambda name: (name.count("/"), len(name)))
        prefix = skill_md_name[: -len("SKILL.md")]
        wrote = False
        for archive_name in names:
            if prefix and not archive_name.startswith(prefix):
                continue

            rel_name = archive_name[len(prefix):] if prefix else archive_name
            if not rel_name:
                continue

            output_path = target_dir / rel_name
            if archive_name.endswith("/"):
                output_path.mkdir(parents=True, exist_ok=True)
                continue

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(archive_name, "r") as src, open(output_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            wrote = True
        return wrote

    def _download_repo_archive(self, repo: dict) -> Optional[bytes]:
        """
        下载市场仓库压缩包，并在缺省分支之间做回退尝试。
        """
        branches = [repo.get("branch")] if repo.get("branch") else []
        branches.extend([branch for branch in _DEFAULT_BRANCHES if branch not in branches])
        for branch in branches:
            archive_url = (
                f"https://codeload.github.com/{repo['owner']}/{repo['repo']}/zip/refs/heads/{branch}"
            )
            response = self._request_github(
                url=archive_url,
                repo_name=repo["repo_name"],
                is_api=False,
            )
            if response is not None and response.status_code == 200:
                repo["branch"] = branch
                return response.content
        logger.warning("下载技能市场仓库失败：%s", repo["repo_url"])
        return None

    @staticmethod
    def _request_convex_query(
        deployment_url: str,
        path: str,
        args: dict,
        timeout: int = 30,
    ):
        """
        以官方前端相同的请求格式调用 Convex query 接口。
        """
        headers = {
            "Content-Type": "application/json",
            "Convex-Client": _CLAWHUB_CONVEX_CLIENT,
        }
        payload = {
            "path": path,
            "format": "convex_encoded_json",
            "args": [args or {}],
        }

        strategies = []
        if settings.PROXY_HOST:
            strategies.append({"proxies": settings.PROXY, "timeout": timeout})
        strategies.append({"timeout": timeout})

        for kwargs in strategies:
            try:
                response = RequestUtils(headers=headers, **kwargs).post_res(
                    url=f"{deployment_url.rstrip('/')}/api/query",
                    json=payload,
                    raise_exception=True,
                )
                if response is not None and response.status_code == 200:
                    return response
            except Exception as e:
                logger.warning(
                    "请求 Convex 技能列表失败：%s/api/query - %s",
                    deployment_url.rstrip("/"),
                    e,
                )
        return None

    @staticmethod
    def _request_registry(
        url: str,
        params: Optional[dict] = None,
        timeout: int = 30,
    ):
        """
        请求注册表 API，兼容代理和直连场景。
        """
        strategies = []
        if settings.PROXY_HOST:
            strategies.append(({"proxies": settings.PROXY, "timeout": timeout}, url))
        strategies.append(({"timeout": timeout}, url))

        for kwargs, target_url in strategies:
            try:
                response = RequestUtils(**kwargs).get_res(
                    url=target_url,
                    params=params,
                    raise_exception=True,
                )
                if response is not None and response.status_code == 200:
                    return response
            except Exception as e:
                logger.warning("请求注册表技能市场失败：%s - %s", target_url, e)
        return None

    @staticmethod
    def _request_github(
        url: str,
        repo_name: str,
        is_api: bool = False,
        timeout: int = 30,
    ):
        """
        按代理优先级顺序请求 GitHub 资源，兼容代理和直连场景。
        """
        strategies = []
        headers = settings.REPO_GITHUB_HEADERS(repo=repo_name)
        if not is_api and settings.GITHUB_PROXY:
            proxy_url = f"{UrlUtils.standardize_base_url(settings.GITHUB_PROXY)}{url}"
            strategies.append((proxy_url, {"headers": headers, "timeout": timeout}))
        if settings.PROXY_HOST:
            strategies.append(
                (
                    url,
                    {
                        "headers": headers,
                        "proxies": settings.PROXY,
                        "timeout": timeout,
                    },
                )
            )
        strategies.append((url, {"headers": headers, "timeout": timeout}))

        for target_url, kwargs in strategies:
            try:
                response = RequestUtils(**kwargs).get_res(
                    url=target_url,
                    raise_exception=True,
                )
                if response is not None and response.status_code == 200:
                    return response
            except Exception as e:
                logger.warning("请求技能市场失败：%s - %s", target_url, e)
        return None
