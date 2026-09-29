# 音频容器测试样本

`silence.flac` 是本地生成的三秒静音音频：44.1 kHz、16 bit、双声道，移除了音频标签和填充区。
它不含第三方录音内容，用于离线验证 Mutagen 对真实无标签 FLAC 的流参数读取。
测试先复制到临时目录，再写入标签，不修改此原始样本，也不要求 CI 安装 ffmpeg。

生成过程：标准库 `wave` 写入三秒零值 PCM，通过 ffmpeg 的 `flac` 编码器转换，再使用
`mutagen.flac.FLAC.clear()` 和 `save(padding=lambda _: 0)` 清除标签及填充。
