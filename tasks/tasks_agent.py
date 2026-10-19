"""Agent 讲题异步任务入口（Q2 agent 集群消费）

业务实现放在 teaching/agent_service.py（OJ 内部模块直接调用函数），
本模块只作为 Q2 的稳定函数路径：tasks.tasks_agent.runTutorAgent
"""
from teaching.agent_service import runTutorAgent

__all__ = ['runTutorAgent']
