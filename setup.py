from setuptools import setup, Extension
from Cython.Build import cythonize
import glob

# 递归获取所有.py文件
sources = glob.glob("app/**/*.py", recursive=True)

# 移除不需要编译的文件
sources.remove("app/main.py")

# 配置编译参数（可选优化选项）
extensions = [
    Extension(
        name=path.replace("/", ".").replace(".py", ""),
        sources=[path],
        extra_compile_args=["-O3", "-ffast-math"],
    )
    for path in sources
]

setup(
    name="MoviePilot",
    author="jxxghp",
    ext_modules=cythonize(
        extensions,
        build_dir="build",
        compiler_directives={
            "language_level": "3",
            "auto_pickle": False,
            "embedsignature": True,
            "annotation_typing": True,
            "infer_types": False
        },
        annotate=True
    ),
    script_args=["build_ext", "-j8", "--inplace"],
)
