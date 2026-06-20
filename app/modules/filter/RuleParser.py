import threading

from pyparsing import Forward, Literal, Word, alphas, infix_notation, opAssoc, alphanums, Combine, nums, ParseResults

from app.utils import rust_accel


class RuleParser:

    _lock = threading.Lock()
    _thread_local = threading.local()

    def __init__(self):
        """
        定义语法规则
        """
        with self._lock:
            if not hasattr(self._thread_local, 'initialized'):
                # 表达式
                expr: Forward = Forward()
                # 原子
                atom: Combine = Combine(Word(alphas, alphanums) | (Word(nums) + Word(alphas, alphanums)))
                # 逻辑非操作符
                operator_not: Literal = Literal('!').set_parse_action(lambda t: 'not')
                # 逻辑或操作符
                operator_or: Literal = Literal('|').set_parse_action(lambda t: 'or')
                # 逻辑与操作符
                operator_and: Literal = Literal('&').set_parse_action(lambda t: 'and')
                # 定义表达式的语法规则
                expr <<= (operator_not + expr) | atom | ('(' + expr + ')')

                # 运算符优先级
                self.expr = infix_notation(expr,
                                          [(operator_not, 1, opAssoc.RIGHT),
                                           (operator_and, 2, opAssoc.LEFT),
                                           (operator_or, 2, opAssoc.LEFT)])

                self._thread_local.expr = self.expr
                self._thread_local.initialized = True
            else:
                self.expr = self._thread_local.expr

    def parse(self, expression: str) -> ParseResults:
        """
        解析给定的表达式。

        参数:
        expression -- 要解析的表达式

        返回:
        解析结果
        """
        rust_result = rust_accel.parse_filter_rule(expression)
        if rust_result is not None:
            return _RustParseResults(rust_result)
        return self.expr.parse_string(expression)


class _RustParseResults(list):
    """
    包装 Rust 解析结果，提供本模块调用方使用的 as_list/asList 接口。
    """

    def as_list(self) -> list:
        """
        返回兼容 pyparsing.ParseResults.as_list 的列表结构。
        """
        return list(self)

    def asList(self) -> list:  # noqa: N802
        """
        返回兼容 pyparsing.ParseResults.asList 的列表结构。
        """
        return self.as_list()


if __name__ == '__main__':
    # 测试代码
    expression_str = """
     SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > SPECSUB & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & WEBDL & !DOLBY & !3D > CNSUB & 4K & WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > 4K & !BLU & !REMUX & !DOLBY & HDR & !3D > 4K & !BLURAY & !REMUX & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & !3D > CNSUB & 1080P & WEBDL & !DOLBY & !3D > 1080P & !BLU & !REMUX & !DOLBY & HDR & !3D > 1080P & !BLU & !REMUX & !DOLBY & !3D
    """
    for exp in expression_str.split('>'):
        parsed_expr = RuleParser().parse(exp.strip())
        print(parsed_expr.asList())
