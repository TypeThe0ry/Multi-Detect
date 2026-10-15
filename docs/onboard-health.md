# 机载思考系统健康处置

Multi-Detect 的快速系统负责持续提出候选目标，慢速系统是本机运行的
`Qwen3-VL-2B` llama-server。飞行控制的设定点泵与慢速推理相互独立；慢模型等待时，
飞机继续发送安全的悬停设定点。

策略每帧先经过健康门。慢调用连续失败两次后，该模型被标记为不可用，当前帧立即悬停。
操作系统负责 llama-server 的生命周期，服务单元是
[`deploy/jetson/llama-server.service`](../deploy/jetson/llama-server.service)，使用
`Restart=on-failure`、两秒重试。策略进程不会创建重启线程、锁或子进程；模型恢复后，
健康探测最多每五秒一次，探测成功并完成一次成功推理才会恢复放行。

健康门还读取磁盘剩余空间、温度/降频和内存压力。低于硬盘下限时停止写入帧文件，跳过慢模型调用并悬停
（该跳过不计为慢模型失败）；需要图像的任务若持续异常达到配置的升级时间（默认 60 秒），执行返航。温度或内存压力会让检测器
隔帧运行，跳过检测的帧保持悬停；压力持续达到同一时间后返航。任何需要返航或降落的决定
先交给 `MissionSupervisor`，因此 `land_now` 的优先级始终高于返航；没有 supervisor 时
才使用执行器的返航原语。

每种状态转换只记录一次。日志写入即使失败也不会打断控制循环。部署时请把 systemd 单元中的
`LLAMA_MODEL_PATH` 和 `LLAMA_MMPROJ_PATH` 替换为目标机上的只读模型路径，不要在单元或环境
文件中放入密钥：服务只监听 `127.0.0.1:9933`。

安装和检查：

```bash
sudo install -m 0644 deploy/jetson/llama-server.service /etc/systemd/system/llama-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now llama-server.service
systemd-analyze verify /etc/systemd/system/llama-server.service
systemctl status llama-server.service
curl --fail --max-time 1 http://127.0.0.1:9933/health
```

在 SIM 或评估环境中不启动或重启该服务；可注入假的探测器和资源读取器验证策略状态机。
