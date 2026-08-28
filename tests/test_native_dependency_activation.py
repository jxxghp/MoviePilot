"""已加载原生发行包的磁盘替换检测。"""

from pathlib import Path

from app.runtime import native_dependencies


class _Distribution:
    """提供 importlib.metadata.Distribution 的最小文件契约。"""

    def __init__(self, root: Path, *, version: str) -> None:
        self.root = root
        self.version = version
        self.metadata = {"Name": "Native_Demo"}
        self.files = (
            Path("native_demo/__init__.py"),
            Path("native_demo/extension.cpython-314-darwin.so"),
            Path("native_demo/.libs/libdemo.2.dylib"),
        )

    def locate_file(self, path: Path) -> Path:
        """把发行包相对路径定位到测试根目录。"""
        return self.root / path


def _write_distribution(root: Path) -> tuple[Path, Path]:
    """创建一个扩展模块及其旁加载本地库。"""
    extension = root / "native_demo/extension.cpython-314-darwin.so"
    library = root / "native_demo/.libs/libdemo.2.dylib"
    extension.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    (root / "native_demo/__init__.py").write_text("", encoding="utf-8")
    extension.write_bytes(b"extension-v1")
    library.write_bytes(b"library-v1")
    return extension, library


def test_detects_changed_sibling_library_for_loaded_extension(tmp_path, monkeypatch):
    """扩展已加载时，同发行包旁加载库被替换也必须要求新进程激活。"""
    extension, library = _write_distribution(tmp_path)
    baseline_distribution = _Distribution(tmp_path, version="1.0.0")
    monkeypatch.setattr(
        native_dependencies,
        "_iter_installed_distributions",
        lambda: (baseline_distribution,),
    )
    monkeypatch.setattr(
        native_dependencies,
        "_loaded_native_paths",
        lambda: {native_dependencies._path_key(extension)},
    )

    baseline = native_dependencies.capture_loaded_native_dependencies()
    library.write_bytes(b"library-version-two")
    current_distribution = _Distribution(tmp_path, version="2.0.0")
    monkeypatch.setattr(
        native_dependencies,
        "distribution",
        lambda _name: current_distribution,
    )

    changes = native_dependencies.detect_changed_native_dependencies(baseline)

    assert len(changes) == 1
    assert changes[0].distribution == "native-demo"
    assert changes[0].previous_version == "1.0.0"
    assert changes[0].current_version == "2.0.0"
    assert changes[0].artifacts == ("libdemo.2.dylib",)


def test_ignores_native_distribution_that_is_not_loaded(tmp_path, monkeypatch):
    """尚未加载的原生包首次安装或更新不需要提示重启。"""
    _write_distribution(tmp_path)
    installed_distribution = _Distribution(tmp_path, version="1.0.0")
    monkeypatch.setattr(
        native_dependencies,
        "_iter_installed_distributions",
        lambda: (installed_distribution,),
    )
    monkeypatch.setattr(native_dependencies, "_loaded_native_paths", set)

    baseline = native_dependencies.capture_loaded_native_dependencies()

    assert baseline.distributions == ()


def test_unchanged_loaded_native_distribution_does_not_require_restart(
    tmp_path,
    monkeypatch,
):
    """依赖安装未改变原生文件时不得产生误报。"""
    extension, _ = _write_distribution(tmp_path)
    installed_distribution = _Distribution(tmp_path, version="1.0.0")
    monkeypatch.setattr(
        native_dependencies,
        "_iter_installed_distributions",
        lambda: (installed_distribution,),
    )
    monkeypatch.setattr(
        native_dependencies,
        "_loaded_native_paths",
        lambda: {native_dependencies._path_key(extension)},
    )
    monkeypatch.setattr(
        native_dependencies,
        "distribution",
        lambda _name: installed_distribution,
    )

    baseline = native_dependencies.capture_loaded_native_dependencies()

    assert native_dependencies.detect_changed_native_dependencies(baseline) == ()


def test_recognizes_versioned_shared_object_names():
    """Linux 常见的 libname.so.N 旁加载库属于原生载荷。"""
    assert native_dependencies._is_native_file("/venv/site-packages/demo/libdemo.so.3")
