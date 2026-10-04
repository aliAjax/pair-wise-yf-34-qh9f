"""监管抽查对账子系统。

分层维护：adjudication 判定（纯函数）、store 存储（全部 SQL 与幂等键）、
service 编排（角色、事务与级联失效）、api 接口入口（HTTP 路由）。
"""
