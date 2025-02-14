from distutils.core import setup

from Cython.Build import cythonize

module_list = ['app/helper/sites.py']

setup(
    name="MoviePilot",
    author="jxxghp",
    zip_safe=False,
    include_package_data=True,
    ext_modules=cythonize(
        module_list=module_list,
        nthreads=0,
        compiler_directives={
            "language_level": "3",
            "binding": False,
            "nonecheck": False
        },
    ),
    script_args=["build_ext", "-j", '2', "--inplace"],
)
