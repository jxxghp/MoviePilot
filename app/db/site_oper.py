from datetime import datetime
from typing import List, Tuple, Optional, Dict

from app.db import DbOper
from app.db.models import SiteIcon
from app.db.models.site import Site
from app.db.models.sitestatistic import SiteStatistic
from app.db.models.siteuserdata import SiteUserData
from app.helper.domain_alias import DomainAliasHelper


class SiteOper(DbOper):
    """
    站点管理
    """

    def __init__(self):
        super().__init__()
        self.domain_alias_helper = DomainAliasHelper()

    def add(self, **kwargs) -> Tuple[bool, str]:
        """
        新增站点
        """
        domain = kwargs.get("domain")
        if not domain:
            return False, "域名不能为空"

        # 检查站点是否已存在（包括别名检查）
        exists, existing_domain = self.exists_with_alias(domain)
        if exists and existing_domain:
            # 先检查是否有重复站点需要合并
            duplicates = self.find_duplicate_sites()
            for group_key, sites in duplicates.items():
                # 检查当前域名是否在某个重复组中
                domain_in_group = any(site['domain'] == domain for site in sites)
                existing_in_group = any(site['domain'] == existing_domain for site in sites)

                if domain_in_group or existing_in_group:
                    # 合并重复站点，保留当前要添加的域名
                    success, merge_message = self.merge_duplicate_sites(keep_domain=domain)
                    if success:
                        # 如果合并成功，检查是否还需要更新域名
                        final_site = self.get_by_domain(domain)
                        if not final_site:
                            # 如果当前域名的站点不存在，可能是被合并了，需要更新保留站点的域名
                            remaining_site = self.get_by_domain(existing_domain)
                            if remaining_site:
                                remaining_site.update(self._db, {"domain": domain})
                                return True, f"已合并重复站点并更新域名到 {domain}。{merge_message}"
                        return True, f"已合并重复站点。{merge_message}"
                    break

            # 如果没有重复站点需要合并，只更新域名
            existing_site = self.get_by_domain(existing_domain)
            if existing_site:
                existing_site.update(self._db, {"domain": domain})
                return True, f"站点已存在，已更新域名从 {existing_domain} 到 {domain}"

        # 直接使用当前域名存储（不再转换为主域名）
        site = Site(**kwargs)
        site.create(self._db)
        return True, "新增站点成功"

    def get(self, sid: int) -> Site:
        """
        查询单个站点
        """
        return Site.get(self._db, sid)

    def list(self) -> List[Site]:
        """
        获取站点列表
        """
        return Site.list(self._db)

    def list_order_by_pri(self) -> List[Site]:
        """
        获取站点列表
        """
        return Site.list_order_by_pri(self._db)

    def list_active(self) -> List[Site]:
        """
        按状态获取站点列表
        """
        return Site.get_actives(self._db)

    def delete(self, sid: int):
        """
        删除站点
        """
        Site.delete(self._db, sid)

    def update(self, sid: int, payload: dict) -> Site:
        """
        更新站点
        """
        site = Site.get(self._db, sid)
        site.update(self._db, payload)
        return site

    def get_by_domain(self, domain: str) -> Site:
        """
        按域名获取站点
        """
        return Site.get_by_domain(self._db, domain)

    def get_domains_by_ids(self, ids: List[int]) -> List[str]:
        """
        按ID获取站点域名
        """
        return Site.get_domains_by_ids(self._db, ids)

    def exists(self, domain: str) -> bool:
        """
        判断站点是否存在
        """
        return Site.get_by_domain(self._db, domain) is not None

    def exists_with_alias(self, domain: str) -> Tuple[bool, Optional[str]]:
        """
        检查站点是否存在（包括别名检查）

        :param domain: 域名
        :return: (是否存在, 存在的域名)
        """
        if not domain:
            return False, None

        # 获取所有相关域名（主域名和别名）
        all_domains = self.domain_alias_helper.get_all_domains(domain)

        # 检查每个域名是否已存在
        for check_domain in all_domains:
            if self.exists(check_domain):
                return True, check_domain

        return False, None



    def update_cookie(self, domain: str, cookies: str) -> Tuple[bool, str]:
        """
        更新站点Cookie
        """
        site = Site.get_by_domain(self._db, domain)
        if not site:
            return False, "站点不存在"
        site.update(self._db, {
            "cookie": cookies
        })
        return True, "更新站点Cookie成功"

    def update_rss(self, domain: str, rss: str) -> Tuple[bool, str]:
        """
        更新站点rss
        """
        site = Site.get_by_domain(self._db, domain)
        if not site:
            return False, "站点不存在"
        site.update(self._db, {
            "rss": rss
        })
        return True, "更新站点RSS地址成功"

    def update_userdata(self, domain: str, name: str, payload: dict) -> Tuple[bool, str]:
        """
        更新站点用户数据
        """
        # 当前系统日期
        current_day = datetime.now().strftime('%Y-%m-%d')
        current_time = datetime.now().strftime('%H:%M:%S')
        payload.update({
            "domain": domain,
            "name": name,
            "updated_day": current_day,
            "updated_time": current_time,
            "err_msg": payload.get("err_msg") or ""
        })
        # 按站点+天判断是否存在数据
        siteuserdatas = SiteUserData.get_by_domain(self._db, domain=domain, workdate=current_day)
        if siteuserdatas:
            # 存在则更新
            if not payload.get("err_msg"):
                siteuserdatas[0].update(self._db, payload)
        else:
            # 不存在则插入
            SiteUserData(**payload).create(self._db)
        return True, "更新站点用户数据成功"

    def get_userdata(self) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.list(self._db)

    def get_userdata_by_domain(self, domain: str, workdate: Optional[str] = None) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.get_by_domain(self._db, domain=domain, workdate=workdate)

    def get_userdata_by_date(self, date: str) -> List[SiteUserData]:
        """
        获取站点用户数据
        """
        return SiteUserData.get_by_date(self._db, date)

    def get_userdata_latest(self) -> List[SiteUserData]:
        """
        获取站点最新数据
        """
        return SiteUserData.get_latest(self._db)

    def get_icon_by_domain(self, domain: str) -> SiteIcon:
        """
        按域名获取站点图标
        """
        return SiteIcon.get_by_domain(self._db, domain)

    def update_icon(self, name: str, domain: str, icon_url: str, icon_base64: str) -> bool:
        """
        更新站点图标
        """
        icon_base64 = f"data:image/ico;base64,{icon_base64}" if icon_base64 else ""
        siteicon = self.get_icon_by_domain(domain)
        if not siteicon:
            SiteIcon(name=name, domain=domain, url=icon_url, base64=icon_base64).create(self._db)
        elif icon_base64:
            siteicon.update(self._db, {
                "url": icon_url,
                "base64": icon_base64
            })
        return True

    def success(self, domain: str, seconds: Optional[int] = None):
        """
        站点访问成功
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = SiteStatistic.get_by_domain(self._db, domain)
        if sta:
            avg_seconds, note = None, {}
            if seconds is not None:
                note: dict = sta.note or {}
                note[lst_date] = seconds or 1
                avg_times = len(note.keys())
                if avg_times > 10:
                    note = dict(sorted(note.items(), key=lambda x: x[0], reverse=True)[:10])
                avg_seconds = sum([v for v in note.values()]) // avg_times
            sta.update(self._db, {
                "success": sta.success + 1,
                "seconds": avg_seconds or sta.seconds,
                "lst_state": 0,
                "lst_mod_date": lst_date,
                "note": note or sta.note
            })
        else:
            note = {}
            if seconds is not None:
                note = {
                    lst_date: seconds or 1
                }
            SiteStatistic(
                domain=domain,
                success=1,
                fail=0,
                seconds=seconds or 1,
                lst_state=0,
                lst_mod_date=lst_date,
                note=note
            ).create(self._db)

    def fail(self, domain: str):
        """
        站点访问失败
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = SiteStatistic.get_by_domain(self._db, domain)
        if sta:
            sta.update(self._db, {
                "fail": sta.fail + 1,
                "lst_state": 1,
                "lst_mod_date": lst_date
            })
        else:
            SiteStatistic(
                domain=domain,
                success=0,
                fail=1,
                lst_state=1,
                lst_mod_date=lst_date
            ).create(self._db)

    def find_duplicate_sites(self) -> Dict[str, List[Dict]]:
        """
        检测数据库中的重复站点（同一站点的不同域名）

        :return: 重复站点分组 {主域名: [站点信息列表]}
        """
        duplicates = {}
        all_sites = self.list()
        processed_domains = set()

        for site in all_sites:
            domain = site.domain
            if domain in processed_domains:
                continue

            # 查找所有相关域名的站点
            related_sites = []
            all_related_domains = self.domain_alias_helper.get_all_domains(domain)

            for check_site in all_sites:
                if check_site.domain in all_related_domains:
                    related_sites.append({
                        'id': check_site.id,
                        'domain': check_site.domain,
                        'name': check_site.name,
                        'is_active': check_site.is_active,
                        'success_count': getattr(check_site, 'success_count', 0) or 0,
                        'fail_count': getattr(check_site, 'fail_count', 0) or 0,
                        'updated_at': getattr(check_site, 'updated_at', None)
                    })
                    processed_domains.add(check_site.domain)

            # 如果有多个相关站点，则认为是重复的
            if len(related_sites) > 1:
                # 使用第一个域名作为组标识
                group_key = related_sites[0]['domain']
                duplicates[group_key] = related_sites

        return duplicates

    def merge_duplicate_sites(self, keep_domain: Optional[str] = None) -> Tuple[bool, str]:
        """
        合并重复的站点

        :param keep_domain: 要保留的域名，如果为None则保留最后更新的
        :return: (是否成功, 消息)
        """
        try:
            duplicates = self.find_duplicate_sites()
            if not duplicates:
                return True, "未发现重复站点"

            merged_count = 0
            for group_key, sites in duplicates.items():
                if len(sites) <= 1:
                    continue

                # 确定要保留的站点
                if keep_domain:
                    # 查找指定域名的站点
                    keep_site = None
                    for site in sites:
                        if site['domain'] == keep_domain:
                            keep_site = site
                            break
                    if not keep_site:
                        continue  # 指定的域名不在这个组中
                else:
                    # 选择最后更新的站点，如果没有更新时间则选择第一个
                    keep_site = sites[0]
                    for site in sites:
                        if site['updated_at'] and (not keep_site['updated_at'] or site['updated_at'] > keep_site['updated_at']):
                            keep_site = site

                # 获取要删除的站点
                remove_sites = [s for s in sites if s['id'] != keep_site['id']]

                # 合并统计数据
                total_success = sum(s['success_count'] for s in sites)
                total_fail = sum(s['fail_count'] for s in sites)

                # 更新保留的站点
                keep_site_obj = self.get(keep_site['id'])
                if keep_site_obj:
                    # 获取当前统计数据
                    current_stats = SiteStatistic.get_by_domain(self._db, keep_site['domain'])
                    if current_stats:
                        current_stats.update(self._db, {
                            'success': total_success,
                            'fail': total_fail
                        })

                # 删除重复的站点
                for remove_site in remove_sites:
                    # 删除站点统计数据
                    remove_stats = SiteStatistic.get_by_domain(self._db, remove_site['domain'])
                    if remove_stats:
                        SiteStatistic.delete(self._db, remove_stats.id)

                    # 删除站点记录
                    Site.delete(self._db, remove_site['id'])
                    print(f"删除重复站点: {remove_site['domain']} (ID: {remove_site['id']})")

                merged_count += len(remove_sites)
                print(f"合并站点组 {group_key}: 保留 {keep_site['domain']}, 删除 {len(remove_sites)} 个重复站点")

            return True, f"成功合并 {merged_count} 个重复站点"

        except Exception as e:
            print(f"合并重复站点失败: {str(e)}")
            return False, f"合并失败: {str(e)}"
