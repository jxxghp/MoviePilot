

class WorkFlowManager:
    """
    工作流管理器
    """
    def __init__(self):
        self.workflows = {}

    def register(self, workflow):
        """
        注册工作流
        :param workflow: 工作流对象
        :return:
        """
        self.workflows[workflow.name] = workflow

    def get_workflow(self, name):
        """
        获取工作流
        :param name: 工作流名称
        :return:
        """
        return self.workflows.get(name)
