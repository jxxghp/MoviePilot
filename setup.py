
from Cython.Build import cythonize
from setuptools import setup

module_list = ['app/helper/sites.py']

setup(
        name="",
        author="",
        zip_safe=False,
        include_package_data=True,
        ext_modules=cythonize(
            module_list=module_list,
            nthreads=0,
            compiler_directives={"language_level": "3"},
        ),
        script_args=["build_ext", "-j", '2', "--inplace"],
    )
