from typing import Any


class ObjectUtils:

    @staticmethod
    def is_obj(obj: Any):
        if isinstance(obj, list) or isinstance(obj, dict):
            return True
        else:
            return str(obj).startswith("{") or str(obj).startswith("[")
