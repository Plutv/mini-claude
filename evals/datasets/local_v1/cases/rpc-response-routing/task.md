# 修复RPC响应路由

`app/router.py`模拟一条TCP连接上的RPC响应和服务端推送事件。并发请求发生乱序响应时，当前实现会把结果交给错误的Future。

要求：

- `register(request_id)`为每个请求创建并返回独立Future。
- 重复注册尚未完成的`request_id`必须抛出`DuplicateRequestError`。
- RPC响应必须按消息`id`恢复正确Future，响应可以乱序到达。
- 未知或重复响应不得影响其他请求。
- `type == "event"`的推送交给异步`on_event`回调，不得误当RPC响应。
- 已完成请求要从Pending注册表清理。
