# 24 - Blue/Green: Lambda Linear Deployment

## LINEAR_10PERCENT_EVERY_1_MINUTE
```
t=0min:  10% → v2 (green),  90% → v1 (blue)
t=1min:  20% → v2,          80% → v1
t=2min:  30% → v2,          70% → v1
...
t=9min: 100% → v2,           0% → v1
```
At any step, if an alarm fires → immediate rollback to 100% v1.

## All linear configs
| Config                              | Increment | Interval |
|-------------------------------------|-----------|----------|
| LINEAR_10PERCENT_EVERY_1_MINUTE     | 10%       | 1 min    |
| LINEAR_10PERCENT_EVERY_2_MINUTES    | 10%       | 2 min    |
| LINEAR_10PERCENT_EVERY_3_MINUTES    | 10%       | 3 min    |
| LINEAR_10PERCENT_EVERY_10_MINUTES   | 10%       | 10 min   |
| ALL_AT_ONCE                         | 100%      | instant  |

## Canary vs Linear vs All-at-once
```
CANARY:      [10%]──5min──[100%]                  (2 steps, fast)
LINEAR:      [10%]─[20%]─[30%]─...─[100%]         (10 steps, gradual)
ALL_AT_ONCE: [100%]                                (1 step, instant)
```

## Alarms monitored
- `ErrorsAlarm` — any Lambda errors → rollback
- `ThrottlesAlarm` — throttle spike → rollback

## Deploy
```bash
pip install -r requirements.txt
cdk deploy
```
