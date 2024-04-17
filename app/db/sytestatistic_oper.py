import json
from datetime import datetime

from app.db import DbOper
from app.db.models.sitestatistic import SiteStatistic


class SiteStatisticOper(DbOper):
    """
    站点统计管理
    """

    def success(self, domain: str, seconds: int = None):
        """
        站点访问成功
        """
        lst_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sta = SiteStatistic.get_by_domain(self._db, domain)
        if sta:
            avg_seconds, note = None, {}
            if seconds:
                note: dict = json.loads(sta.note or "{}")
                note[lst_date] = seconds
                avg_times = len(note.keys())
                avg_seconds = sum([v for v in note.values()]) // avg_times
            sta.update(self._db, {
                "success": sta.success + 1,
                "seconds": avg_seconds or sta.seconds,
                "lst_state": 0,
                "lst_mod_date": lst_date,
                "note": json.dumps(note) if note else sta.note
            })
        else:
            note = {}
            if seconds:
                note = {
                    lst_date: seconds
                }
            SiteStatistic(
                domain=domain,
                success=1,
                fail=0,
                seconds=seconds,
                lst_state=0,
                lst_mod_date=lst_date,
                note=json.dumps(note)
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
