import threading

from pyparsing import Forward, Literal, Word, alphas, infixNotation, opAssoc, alphanums, Combine, nums, ParseResults


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
                operator_not: Literal = Literal('!').setParseAction(lambda t: 'not')
                # 逻辑或操作符
                operator_or: Literal = Literal('|').setParseAction(lambda t: 'or')
                # 逻辑与操作符
                operator_and: Literal = Literal('&').setParseAction(lambda t: 'and')
                # 定义表达式的语法规则
                expr <<= (operator_not + expr) | atom | ('(' + expr + ')')

                # 运算符优先级
                self.expr = infixNotation(expr,
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
        return self.expr.parseString(expression)


if __name__ == '__main__':
    # 测试代码
    expression_str = """
     SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 4K & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & CNVOI & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > CNSUB & 4K & WEBDL & 60FPS & !DOLBY & !SDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & WEBDL & !DOLBY & !3D > SPECSUB & 4K & WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & WEBDL & !DOLBY & !3D > CNSUB & 4K & WEBDL & !DOLBY & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & CNVOI & 4K & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > CNSUB & 4K & !BLU & !WEBDL & !DOLBY & !SDR & !3D > 4K & !BLU & !REMUX & !DOLBY & HDR & !3D > 4K & !BLURAY & !REMUX & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !REMUX & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > CNSUB & 1080P & !BLU & !WEBDL & !DOLBY & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > CNSUB & 1080P & WEBDL & !DOLBY & HDR & !3D > SPECSUB & 1080P & WEBDL & !DOLBY & !3D > CNSUB & 1080P & WEBDL & !DOLBY & !3D > 1080P & !BLU & !REMUX & !DOLBY & HDR & !3D > 1080P & !BLU & !REMUX & !DOLBY & !3D  
    """
    for exp in expression_str.split('>'):
        parsed_expr = RuleParser().parse(exp.strip())
        print(parsed_expr.asList())
