# encoding: utf-8
import sys


class AsObj:
    def __init__(self, json=None, key=None, dict_key=False, dict_key_name=None):
        self._json = json if json else {}
        self._key = key
        self._dict_key = dict_key
        self._dict_key_name = dict_key_name
        self._obj_list = []
        self._list_only = False
        if isinstance(self._json, list):
            self._obj_list = [AsObj(o) if isinstance(o, (dict, list)) else o for o in self._json]
            self._list_only = True
        elif dict_key:
            self._obj_list = [
                AsObj({k: v}, key=k, dict_key_name=dict_key_name) if isinstance(v, (dict, list)) else v
                for k, v in self._json.items()
            ]
            self._list_only = True
        else:
            for key, value in self._json.items():
                if isinstance(value, (dict, list)):
                    if self._key and key == self._key:
                        final = AsObj(value, dict_key=isinstance(value, dict), dict_key_name=key)
                        self._obj_list = final
                    else:
                        final = AsObj(value)
                else:
                    final = value
                if dict_key_name:
                    setattr(self, dict_key_name, key)
                setattr(self, key, final)

    def _dict(self):
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

    def to_dict(self):
        return self._dict()

    def __delitem__(self, key):
        return delattr(self, key)

    def __getitem__(self, key):
        if isinstance(key, int) and self._obj_list:
            return self._obj_list[key]
        else:
            return getattr(self, key)

    def __iter__(self):
        return (o for o in self._obj_list) if self._obj_list else iter(self._dict())

    def __len__(self):
        return len(self._obj_list) if self._obj_list else len(self._dict())

    def __repr__(self):
        return str(self._obj_list) if self._list_only else str(self._dict())

    def __setitem__(self, key, value):
        return setattr(self, key, value)
    
    def __str__(self):
        return str(self._obj_list) if self._list_only else str(self._dict())

    if sys.version_info >= (3, 8):
        def __reversed__(self):
            return reversed(self._dict())

    if sys.version_info >= (3, 9):
        def __class_getitem__(cls, key):
            return cls.__dict__.__class_getitem__(key)

        def __ior__(self, value):
            return self._dict().__ior__(value)

        def __or__(self, value):
            return self._dict().__or__(value)

    def copy(self):
        return AsObj(self._json.copy(), key=self._key, dict_key=self._dict_key, dict_key_name=self._dict_key_name)

    def get(self, key, value=None):
        return self._dict().get(key, value)

    def items(self):
        return self._dict().items()

    def keys(self):
        return self._dict().keys()

    def pop(self, key, value=None):
        return self.__dict__.pop(key, value)
    
    def popitem(self):
        return self.__dict__.popitem()
    
    def setdefault(self, key, value=None):
        return self.__dict__.setdefault(key, value)

    def update(self, entries):
        return self.__dict__.update(entries)

    def values(self):
        return self._dict().values()
