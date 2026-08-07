---
description: 紧急制动：停止一切自动推进（/stop）
---
创建 data/STOP 文件（空文件即可），然后向用户报告当前各 worker 与队列状态。调度纪律在此文件存在时停止一切复活/分派/nudge。恢复运行：/autorun on（会自动删除该文件）。
