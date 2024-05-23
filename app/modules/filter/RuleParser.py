from pyparsing import Forward, Literal, Word, alphas, infixNotation, opAssoc, alphanums, Combine, nums, ParseResults


class RuleParser:

    def __init__(self):
        """
        定义语法规则
        """
        # 表达式
        expr: Forward = Forward()
        # 原子
        atom: Combine = Combine(Word(alphas, alphanums) | Word(nums) + Word(alphas, alphanums))
        # 逻辑非操作符
        operator_not: Literal = Literal('!').setParseAction(lambda t: 'not')
        # 逻辑或操作符
        operator_or: Literal = Literal('|').setParseAction(lambda t: 'or')
        # 逻辑与操作符
        operator_and: Literal = Literal('&').setParseAction(lambda t: 'and')
        # 定义表达式的语法规则
        expr <<= operator_not + expr | operator_or | operator_and | atom | ('(' + expr + ')')

        # 运算符优先级
        self.expr = infixNotation(expr,
                                  [(operator_not, 1, opAssoc.RIGHT),
                                   (operator_and, 2, opAssoc.LEFT),
                                   (operator_or, 2, opAssoc.LEFT)])

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
    expression_str = "!BLU & 4K & CN > !BLU & 1080P & CN > !BLU & 4K > !BLU & 1080P"
    for exp in expression_str.split('>'):
        parsed_expr = RuleParser().parse(exp)
        print(parsed_expr.as_list())
